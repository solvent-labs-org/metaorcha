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
   naming the check that failed (story 2.5), or ``attested_unsettled`` when
   no gate decision is recorded (the flag is off, or the gate has not
   evaluated the run). The gate runs inside the turn, before its ``done``
   event, so its row is already written when the firing reads it.

A crash after the claim skips that slot; it is never repeated. After a
restart, before the first claim, ``sweep_restart`` settles what the previous
process left (AD-22): a firing whose run sealed keeps its receipt and gets
its gate judgement; a ``running`` one with nothing sealed is ``error`` /
``restart``; an unsealed ``paused`` one is left for its owner. The sweep
assumes one scheduler instance per database (logged at start); with several,
a restarting instance would also sweep the others' in-flight rows.

What a restart still loses: a deferred settle held in process memory, and —
with the gate on — the gate's decision for a run sealed just before the
crash, which then reads ``attested_unsettled``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from common.utils.src import firing_view

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


def _gate_flag() -> bool:
    from ..config import settings  # noqa: PLC0415 — read at call time

    return bool(getattr(settings, "settlement_require_attestation", False))


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
        gate_on: Callable[[], bool] = _gate_flag,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._poll_interval = poll_interval
        self._runner = runner
        self._db = db
        self._check_connection = check_connection
        self._create_session = create_session
        self._flags = flags
        self._gate_on = gate_on
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._firings: set[asyncio.Task[None]] = set()
        self._running = False
        # The restart sweep runs in the loop, before the first claim; rows it
        # could not settle are retried by id on later polls.
        self._sweep_pending = True
        self._unswept: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._sweep_pending = True
        logger.info(
            "WorkflowScheduler: one scheduler per database is assumed; the "
            "restart sweep judges every unfinished firing it finds"
        )
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
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WorkflowScheduler tick error")
            await asyncio.sleep(self._poll_interval)

    async def _poll(self) -> None:
        """One loop pass: the restart sweep until it completes, then a tick.

        No slot is claimed while the sweep is pending, so a full sweep never
        meets a firing of this process.
        """
        if self._sweep_pending:
            await self.sweep_restart()
        elif self._unswept:
            await self.sweep_restart(only=set(self._unswept))
        if not self._sweep_pending:
            await self._tick()

    # ── restart ───────────────────────────────────────────────────────────

    async def sweep_restart(self, only: set[str] | None = None) -> int:
        """Settle what a previous process left unfinished (AD-22). Returns rows updated.

        - ``scheduled``, never started: ``error`` / ``restart``.
        - ``running`` / ``paused`` with a sealed envelope in its session: the
          run's gate judgement (``settled`` / ``refused — <check>`` /
          ``attested_unsettled``) with its run_id. A first turn that pauses
          never seals, so a seal in a paused firing's session means its
          resume completed.
        - ``running`` with nothing sealed: ``error`` / ``restart``.
        - ``paused`` with nothing sealed: left alone (its owner may resume it).
        - ``attested_unsettled`` with a gate row for its run: reconciled to it.

        A row that fails is logged and retried by id on the next poll; one
        row never aborts the sweep. ``only`` limits the pass to those ids.
        """
        scope: dict[str, Any] = {"id": {"in": sorted(only)}} if only is not None else {}
        updated = 0
        async with self._db() as db:
            if only is None:
                updated += await db.routinefiring.update_many(
                    where={"state": rules.SCHEDULED, "run_id": None},
                    data={"state": rules.ERROR, "detail": rules.RESTART},
                )
            rows = await db.routinefiring.find_many(
                where={
                    "state": {"in": [rules.RUNNING, rules.PAUSED]},
                    "run_id": None,
                    **scope,
                }
            )
            if only is not None:
                self._unswept &= {row.id for row in rows}
            for row in rows:
                try:
                    run_id = await sealed_run_id(db, row.session_id)
                    if run_id:
                        outcome = await _judged(
                            db, rules.Outcome(rules.ATTESTED_UNSETTLED, None, run_id)
                        )
                    elif row.state == rules.RUNNING:
                        outcome = rules.Outcome(rules.ERROR, rules.RESTART)
                    else:
                        self._unswept.discard(row.id)
                        continue
                    # Compare-and-set: never overwrite a row that moved since it was read.
                    updated += await db.routinefiring.update_many(
                        where={"id": row.id, "state": row.state, "run_id": None},
                        data={
                            "state": outcome.state,
                            "detail": outcome.detail,
                            "run_id": outcome.run_id,
                        },
                    )
                    self._unswept.discard(row.id)
                except Exception:
                    logger.exception(
                        "restart sweep: firing %s left for the next poll", row.id
                    )
                    self._unswept.add(row.id)
            # With the gate off no gate row is written, so nothing can be
            # reconciled: skip the scan of every firing it ever left unsettled.
            if only is None and self._gate_on():
                updated += await _reconcile(db)
        if only is None:
            self._sweep_pending = False
        return updated

    # ── claim ─────────────────────────────────────────────────────────────

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
                await self._recover(firing.id)

    async def _recover(self, firing_id: str) -> None:
        """The end state of a firing whose own run raised or whose last write failed.

        If its run sealed, the firing keeps that receipt and its gate
        judgement; otherwise (or if the seal cannot be read) ``error`` /
        ``internal``.
        """
        async with self._db() as db:
            outcome = rules.Outcome(rules.ERROR, rules.INTERNAL)
            try:
                row = await db.routinefiring.find_unique(where={"id": firing_id})
                run_id = await sealed_run_id(db, getattr(row, "session_id", None))
                if run_id:
                    outcome = await _judged(
                        db, rules.Outcome(rules.ATTESTED_UNSETTLED, None, run_id)
                    )
            except Exception:
                logger.exception(
                    "routine firing %s: seal not read on recovery", firing_id
                )
            await db.routinefiring.update(
                where={"id": firing_id},
                data={
                    "state": outcome.state,
                    "detail": outcome.detail,
                    "run_id": outcome.run_id,
                },
            )

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


async def sealed_run_id(db: Any, session_id: str | None) -> str | None:
    """The run_id of the newest sealed run envelope in ``session_id``, or None.

    Case attestations (run_id NULL) are not seals and are skipped. One row is
    read. Raises on a database error; every caller catches.
    """
    if not session_id:
        return None
    row = await db.attestation.find_first(
        where={"session_id": session_id, "NOT": [{"run_id": None}]},
        order={"created_at": "desc"},
    )
    run_id = getattr(row, "run_id", None)
    return run_id if isinstance(run_id, str) and run_id else None


async def _failing_names(db: Any, rows: list[Any], run_id: str) -> list[str]:
    """The failing signed verdicts' names, when the gate refused for ``verdict_fail``.

    Never raises: with the envelope unread the firing keeps ``verdict_fail``,
    and its judgement is never lost to this read.
    """
    row = firing_view.deciding_row(rows)
    if (
        row is None
        or getattr(row, "outcome", None) != rules.REFUSED
        or firing_view.VERDICT_FAIL not in firing_view.gate_checks(row)
        or firing_view.envelope_untrusted(row)
    ):
        return []
    try:
        # validator is an optional workspace package; imported here so its
        # absence costs only the names.
        from validator.run_envelope import (  # noqa: PLC0415
            get_run_attestation_by_run_id,
        )

        envelope = await get_run_attestation_by_run_id(run_id, db=db)
        return [v["check"] for v in firing_view.failing_verdicts(envelope)]
    except Exception:
        logger.warning("routine firing: failing verdicts not read (%s)", run_id)
        return []


async def _judged(db: Any, outcome: rules.Outcome) -> rules.Outcome:
    """The outcome refined by the gate's rows for its run. Never raises.

    A failed read leaves the firing ``attested_unsettled``: the run is
    sealed either way, the ledger still holds the gate's row, and the next
    restart sweep reconciles the two.
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
    failing = await _failing_names(db, rows, outcome.run_id)
    return rules.judged(outcome, rows, failing)


_RECONCILE_CHUNK = 500


async def _reconcile(db: Any) -> int:
    """Move ``attested_unsettled`` firings whose run has a gate row onto it. Never raises.

    The row read when the firing ended may have failed; the ledger is the
    gate's record. A firing with no gate row stays ``attested_unsettled``.
    """
    updated = 0
    try:
        firings = [
            f
            for f in await db.routinefiring.find_many(
                where={"state": rules.ATTESTED_UNSETTLED}
            )
            if getattr(f, "run_id", None)
        ]
        for start in range(0, len(firings), _RECONCILE_CHUNK):
            chunk = firings[start : start + _RECONCILE_CHUNK]
            ledger = await db.attestedsettlement.find_many(
                where={"run_id": {"in": [f.run_id for f in chunk]}},
                order={"created_at": "asc"},
            )
            by_run: dict[str, list[Any]] = {}
            for row in ledger:
                by_run.setdefault(row.run_id, []).append(row)
            for firing in chunk:
                rows = by_run.get(firing.run_id)
                if not rows:
                    continue
                failing = await _failing_names(db, rows, firing.run_id)
                outcome = rules.judged(
                    rules.Outcome(rules.ATTESTED_UNSETTLED, None, firing.run_id),
                    rows,
                    failing,
                )
                if outcome.state == rules.ATTESTED_UNSETTLED:
                    continue
                updated += await db.routinefiring.update_many(
                    where={
                        "id": firing.id,
                        "state": rules.ATTESTED_UNSETTLED,
                        "run_id": firing.run_id,
                    },
                    data={"state": outcome.state, "detail": outcome.detail},
                )
    except Exception:
        logger.exception("restart sweep: reconcile with the ledger failed")
    return updated


async def record_resume(
    session_id: str,
    events: list[dict[str, Any]],
    db: DbFactory = _prisma,
) -> None:
    """After a resume in a firing's session, move its paused row on. Never raises.

    The owner approved or declined the paused call from the firing's session;
    the turn then ran on, and its end decides the row exactly as the first
    run's did. A session that is not a paused firing's is left alone.

    A restart sweep that ran while this resume was sealing may already have
    moved the row to ``attested_unsettled`` with this run's id, before the
    gate wrote its row: that row is re-judged (compare-and-set), never a
    row of another run.
    """
    try:
        async with db() as conn:
            firing = await conn.routinefiring.find_first(
                where={
                    "session_id": session_id,
                    "state": {"in": [rules.PAUSED, rules.ATTESTED_UNSETTLED]},
                }
            )
            if firing is None:
                return
            outcome = await _judged(conn, rules.outcome_of(events))
            if firing.state == rules.ATTESTED_UNSETTLED:
                if (
                    not outcome.run_id
                    or outcome.run_id != firing.run_id
                    or outcome.state == rules.ATTESTED_UNSETTLED
                ):
                    return
                await conn.routinefiring.update_many(
                    where={
                        "id": firing.id,
                        "state": rules.ATTESTED_UNSETTLED,
                        "run_id": firing.run_id,
                    },
                    data={"state": outcome.state, "detail": outcome.detail},
                )
                return
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
