"""WorkflowScheduler — fires saved routines on their schedule (story 2.2).

Runs inside the SuperAgent process. A firing is a chat turn with no human
typing (AD-19): the routine's goal goes through the same ``SessionRunner``,
pipeline, observers, envelope and gate as a chat turn — there is no second
runner (NFR-7, AD-8).

Each tick, for every routine whose ``next_run_at`` is due:

1. **Claim the slot** by compare-and-set: advance ``next_run_at`` to the next
   slot ``WHERE next_run_at = <the value read>``. Zero rows updated means
   another scheduler instance claimed it, and this one does nothing.
2. **Write the firing row** ``(routine_id, slot, run_id NULL, state)`` —
   ``skipped`` if a previous firing of the routine is still scheduled,
   running or paused (no second concurrent firing), else ``scheduled``.
   ``(routine_id, slot)`` is unique, so a slot is fired at most once.
3. **Fire** (a task per firing): refuse before anything is called if the
   owner left the office (FR-34), connections or attestation are off, or a
   connection is revoked or has no token — the firing is ``error`` with the
   named reason. Otherwise create the session in the routine's office,
   mark the row ``running``, and run the turn as the routine's owner.
4. **Record how it ended** (``firing_rules.outcome_of``): ``paused`` on an
   approval card (resumed from the firing's session; ``record_resume``
   updates the row), ``error``, or the sealed run_id — and then the settle
   gate's judgement of that run (story 2.3, AD-12): ``settled``, ``refused``
   with the failed checks, or ``attested_unsettled`` when the gate is off.
   The gate runs inside the turn, before its ``done`` event, so its row is
   already written when the firing reads it.

A crash after the claim skips that slot; it is never repeated. On start, a
row left ``scheduled`` or ``running`` with no run_id is marked ``error`` with
detail ``restart`` (AD-22). That sweep assumes one scheduler instance per
database; with several, a restarting instance would also mark the others'
in-flight rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import firing_rules as rules

logger = logging.getLogger(__name__)

# (did, user_id) → None, or a named reason the connection cannot be called.
ConnectionCheck = Callable[[str, str], Awaitable[str | None]]
DbFactory = Callable[[], contextlib.AbstractAsyncContextManager[Any]]


@contextlib.asynccontextmanager
async def _prisma() -> AsyncIterator[Any]:
    from src.generated_client import Prisma  # noqa: PLC0415

    db = Prisma()
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()


async def _preflight_connection(did: str, user_id: str) -> str | None:
    """PreFlight's own connection checks, before anything is called.

    Fresh Registry read (revoked / unavailable) and the owner's token for it
    (``credential_missing``) — the same refusals a chat call would meet,
    raised here so a firing that cannot succeed calls nothing.
    """
    from ..middleware.preflight import PreFlightError, PreFlightManager  # noqa: PLC0415
    from ..vault.client import VaultClient  # noqa: PLC0415

    try:
        await PreFlightManager(VaultClient())._assert_connection_callable(
            did, user_id, {}, ""
        )
    except PreFlightError as exc:
        return str(exc)
    return None


async def _create_session(
    session_id: str, user_id: str, title: str, office_id: str | None
) -> None:
    from ..persistence.transcript_store import (  # noqa: PLC0415
        upsert_conversation_session,
    )

    await upsert_conversation_session(session_id, user_id, title, office_id)


def _flags() -> tuple[bool, bool]:
    from ..config import settings  # noqa: PLC0415 — read at call time

    return (
        bool(getattr(settings, "connections_enabled", False)),
        bool(getattr(settings, "run_attestation_enabled", False)),
    )


class WorkflowScheduler:
    """Polls for due routines, claims each slot once, and fires it."""

    def __init__(
        self,
        poll_interval: int = 60,
        *,
        runner: Any = None,
        db: DbFactory = _prisma,
        check_connection: ConnectionCheck = _preflight_connection,
        create_session: Callable[..., Awaitable[None]] = _create_session,
        flags: Callable[[], tuple[bool, bool]] = _flags,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._poll_interval = poll_interval
        self._runner = runner
        self._db = db
        self._check_connection = check_connection
        self._create_session = create_session
        self._flags = flags
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._firings: set[asyncio.Task[None]] = set()
        self._running = False

    async def start(self) -> None:
        self._running = True
        try:
            await self.sweep_restart()
        except Exception:
            logger.exception("WorkflowScheduler: restart sweep failed")
        self._task = asyncio.create_task(self._loop(), name="workflow-scheduler")
        logger.info(
            "WorkflowScheduler started (poll_interval=%ds)", self._poll_interval
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("WorkflowScheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WorkflowScheduler tick error")
            await asyncio.sleep(self._poll_interval)

    # ── claim ─────────────────────────────────────────────────────────────

    async def sweep_restart(self) -> int:
        """Mark firings a previous process left unfinished as ``error``."""
        async with self._db() as db:
            return await db.routinefiring.update_many(
                where={
                    "state": {"in": [rules.SCHEDULED, rules.RUNNING]},
                    "run_id": None,
                },
                data={"state": rules.ERROR, "detail": rules.RESTART},
            )

    async def _tick(self) -> list[asyncio.Task[None]]:
        """Claim every due slot and start its firing. Returns the started tasks."""
        if self._runner is None:
            return []  # nothing to fire with (not wired to a runner)
        now = self._clock()
        started: list[asyncio.Task[None]] = []
        async with self._db() as db:
            due = await db.workflowtemplate.find_many(
                where={"schedule_enabled": True, "next_run_at": {"lte": now}}
            )
            for routine in due:
                try:
                    firing = await self.claim(db, routine, now)
                except Exception:
                    logger.exception("routine %s: claim failed", routine.id)
                    continue
                if firing is None:
                    continue
                task = asyncio.create_task(
                    self.fire(routine, firing), name=f"routine-firing-{firing.id}"
                )
                self._firings.add(task)
                task.add_done_callback(self._firings.discard)
                started.append(task)
        return started

    async def claim(self, db: Any, routine: Any, now: datetime) -> Any:
        """Claim the routine's due slot. Returns the firing row to run, or None.

        None when another instance won the slot, or when this slot's row is
        ``skipped`` / ``error`` and there is nothing to run.
        """
        seen = routine.next_run_at
        try:
            following = rules.next_slot(
                routine.schedule_cron or "", routine.schedule_tz or "UTC", now
            )
        except ValueError as exc:
            # A schedule that no longer parses: claim the slot once, turn the
            # schedule off, and say why. Never a hot loop of failed claims.
            won = await db.workflowtemplate.update_many(
                where={"id": routine.id, "next_run_at": seen, "schedule_enabled": True},
                data={"next_run_at": None, "schedule_enabled": False},
            )
            if won:
                await self._write_row(
                    db, routine, seen, rules.ERROR, f"{rules.INVALID_SCHEDULE}: {exc}"
                )
            return None
        won = await db.workflowtemplate.update_many(
            where={"id": routine.id, "next_run_at": seen, "schedule_enabled": True},
            data={"next_run_at": following, "last_run_at": now},
        )
        if not won:
            return None  # another instance claimed this slot
        busy = await db.routinefiring.find_first(
            where={"routine_id": routine.id, "state": {"in": list(rules.IN_PROGRESS)}}
        )
        if busy is not None:
            await self._write_row(db, routine, seen, rules.SKIPPED, rules.OVERLAP)
            return None
        return await self._write_row(db, routine, seen, rules.SCHEDULED, None)

    async def _write_row(
        self, db: Any, routine: Any, slot: Any, state: str, detail: str | None
    ) -> Any:
        try:
            return await db.routinefiring.create(
                data={
                    "routine_id": routine.id,
                    "office_id": routine.office_id,
                    "user_id": routine.user_id,
                    "slot": slot,
                    "state": state,
                    "detail": detail,
                }
            )
        except Exception:
            # (routine_id, slot) is unique: this slot already has its row.
            logger.warning("routine %s: slot %s already recorded", routine.id, slot)
            return None

    # ── fire ──────────────────────────────────────────────────────────────

    async def fire(self, routine: Any, firing: Any) -> None:
        """Run one claimed firing to an end state. Never raises."""
        try:
            await self._fire(routine, firing)
        except Exception:
            logger.exception("routine %s: firing %s failed", routine.id, firing.id)
            with contextlib.suppress(Exception):
                await self._set(firing.id, state=rules.ERROR, detail=rules.INTERNAL)

    async def _fire(self, routine: Any, firing: Any) -> None:
        refusal = await self._refusal(routine)
        if refusal:
            await self._set(firing.id, state=rules.ERROR, detail=refusal)
            return

        session_id = str(uuid.uuid4())
        await self._create_session(
            session_id, routine.user_id, f"Routine: {routine.name}", routine.office_id
        )
        await self._set(firing.id, state=rules.RUNNING, session_id=session_id)

        events: list[dict[str, Any]] = []
        async for event in self._runner.run_turn(
            session_id=session_id,
            user_id=routine.user_id,
            user_message=routine.goal_template,
            model=rules.routine_model(routine),
            acceptance_criteria=rules.routine_criteria(routine),
            routine_context=rules.routine_context(routine, firing.id),
        ):
            if isinstance(event, dict) and event.get("type") in rules.END_EVENTS:
                events.append(event)
        async with self._db() as db:
            outcome = await _judged(db, rules.outcome_of(events))
        await self._set(
            firing.id, state=outcome.state, detail=outcome.detail, run_id=outcome.run_id
        )

    async def _refusal(self, routine: Any) -> str | None:
        """A named reason this firing must call nothing, or None."""
        from ..middleware.connections import CONNECTIONS_DISABLED  # noqa: PLC0415

        connections_on, attestation_on = self._flags()
        if not connections_on:
            return CONNECTIONS_DISABLED
        if not attestation_on:
            return rules.ATTESTATION_OFF
        async with self._db() as db:
            if routine.office_id is not None:
                member = await db.officemember.find_first(
                    where={"office_id": routine.office_id, "user_id": routine.user_id}
                )
                if member is None:
                    return rules.OWNER_REMOVED
            for did in routine.agents_used or []:
                own = await db.agent.find_first(
                    where={
                        "id": did,
                        "user_id": routine.user_id,
                        "office_id": routine.office_id,
                        "is_active": True,
                    }
                )
                if own is None:
                    return rules.CONNECTION_REVOKED.format(did=did)
        for did in routine.agents_used or []:
            reason = await self._check_connection(did, routine.user_id)
            if reason:
                return reason
        return None

    async def _set(self, firing_id: str, **data: Any) -> None:
        async with self._db() as db:
            await db.routinefiring.update(where={"id": firing_id}, data=data)


async def _judged(db: Any, outcome: rules.Outcome) -> rules.Outcome:
    """The outcome refined by the gate's rows for its run. Never raises.

    A failed read leaves the firing ``attested_unsettled``: the run is
    sealed either way, and the ledger still holds the gate's row.
    """
    if outcome.state != rules.ATTESTED_UNSETTLED or not outcome.run_id:
        return outcome
    try:
        rows = await db.attestedsettlement.find_many(
            where={"run_id": outcome.run_id}, order={"created_at": "asc"}
        )
    except Exception:
        logger.exception("routine firing: gate outcome not read (%s)", outcome.run_id)
        return outcome
    return rules.judged(outcome, rows)


async def record_resume(
    session_id: str,
    events: list[dict[str, Any]],
    db: DbFactory = _prisma,
) -> None:
    """After a resume in a firing's session, move its paused row on. Never raises.

    The owner approved or declined the paused call from the firing's session;
    the turn then ran on, and its end decides the row exactly as the first
    run's did. A session that is not a paused firing's is left alone.
    """
    try:
        async with db() as conn:
            firing = await conn.routinefiring.find_first(
                where={"session_id": session_id, "state": rules.PAUSED}
            )
            if firing is None:
                return
            outcome = await _judged(conn, rules.outcome_of(events))
            await conn.routinefiring.update(
                where={"id": firing.id},
                data={
                    "state": outcome.state,
                    "detail": outcome.detail,
                    "run_id": outcome.run_id,
                },
            )
    except Exception:
        # the id arrives with the resume request: escape line breaks so it cannot
        # forge a log line
        logger.exception(
            "routine firing: resume outcome not recorded (%s)",
            session_id.replace("\r", "\\r").replace("\n", "\\n"),
        )
