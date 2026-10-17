"""What a routine firing is, without I/O (story 2.2, AD-19, AD-22).

The scheduler (``workflow/scheduler.py``) does the reading and writing; these
functions decide. Kept pure so the decisions are tested directly and can be
reused by any other scheduler shape.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.utils.src import firing_view

# AD-22: the one state vocabulary. Nothing else invents a state.
SCHEDULED = "scheduled"
RUNNING = "running"
ATTESTED_UNSETTLED = "attested_unsettled"
SETTLED = "settled"
REFUSED = "refused"
PAUSED = "paused"
SKIPPED = "skipped"
ERROR = "error"
STATES = (
    SCHEDULED,
    RUNNING,
    ATTESTED_UNSETTLED,
    SETTLED,
    REFUSED,
    PAUSED,
    SKIPPED,
    ERROR,
)

# A firing in one of these blocks the routine's next slot: a second
# concurrent firing never starts. A paused firing waits for its owner.
IN_PROGRESS = (SCHEDULED, RUNNING, PAUSED)

# The run_turn / resume events that say how a turn ended.
END_EVENTS = ("interrupt", "error", "stopped", "done")

# Named details. A detail is what the routines pane shows next to the state.
OVERLAP = "skipped: the previous firing of this routine has not finished"
RESTART = "restart"
OWNER_REMOVED = (
    "owner_removed: the routine's owner is no longer a member of its office; "
    "nothing was called"
)
ATTESTATION_OFF = (
    "attestation_off: a routine runs only where its run is sealed "
    "(RUN_ATTESTATION_ENABLED); nothing was called"
)
NO_RECEIPT = "no_receipt: the run finished but no receipt was sealed"
INVALID_SCHEDULE = "invalid_schedule"
CONNECTION_REVOKED = (
    "connection_revoked: {did} is no longer an active connection of the "
    "routine's owner in this office; nothing was called"
)
INTERNAL = "internal: the firing failed before its run finished; see server logs"


def next_slot(cron: str, tz: str, after: datetime) -> datetime:
    """The first slot of ``cron`` in ``tz`` strictly after ``after``, in UTC.

    Read as a standard crontab through the one shared translation
    (``common.utils.src.cron``) the Gateway validated the routine with —
    APScheduler 3.x alone would fire ``* * * * 1`` on Tuesdays.
    Raises ``ValueError`` for a cron or zone that does not parse.
    """
    from common.utils.src.cron import next_slot as _next  # noqa: PLC0415

    return _next(" ".join(cron.split()), tz, after)


def routine_context(routine: Any, firing_id: str) -> dict[str, Any]:
    """The bounds a firing's turn carries in graph state (the scope gate reads them)."""
    parameters = _json(getattr(routine, "parameters", None))
    allow = parameters.get("scope_allow")
    return {
        "routine_id": routine.id,
        "firing_id": firing_id,
        "connections": [str(c) for c in (routine.agents_used or [])],
        "scope_allow": [str(a) for a in allow] if isinstance(allow, list) else [],
        # Story 2.4: run-level criteria (counts_match) read their sources here.
        "criteria_operands": _json(getattr(routine, "criteria_operands", None)),
    }


def routine_model(routine: Any) -> str | None:
    model = _json(getattr(routine, "parameters", None)).get("model")
    return model if isinstance(model, str) and model.strip() else None


def routine_criteria(routine: Any) -> dict[str, Any] | None:
    """The routine's criteria for ``MessageRequest.acceptance_criteria``; None if empty."""
    criteria = _json(getattr(routine, "criteria", None))
    return criteria or None


@dataclass(frozen=True)
class Outcome:
    state: str
    detail: str | None = None
    run_id: str | None = None


def outcome_of(events: Iterable[dict[str, Any]]) -> Outcome:
    """How a turn ended, from the events ``run_turn`` / resume yielded.

    - a resumable interrupt (an approval card, an auth or credit prompt) →
      ``paused``, with what it waits for;
    - an error event → ``error``, with its category;
    - a kill-switch stop → ``error``;
    - ``done`` with a sealed ``run_id`` → ``attested_unsettled`` (``judged``
      refines it by the settle gate's outcome, story 2.3);
    - ``done`` with no receipt → ``error`` (``no_receipt``).
    """
    interrupt: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    stopped = False
    done: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "interrupt":
            interrupt = event
        elif kind == "error":
            error = event
        elif kind == "stopped":
            stopped = True
        elif kind == "done":
            done = event
    if error is not None:
        category = str(error.get("category") or "internal")
        message = str(error.get("error") or "").strip()
        return Outcome(ERROR, f"{category}: {message}" if message else category)
    if stopped:
        return Outcome(ERROR, "stopped: the run was stopped before it finished")
    if interrupt is not None:
        return Outcome(PAUSED, _interrupt_detail(interrupt))
    if done is not None:
        run_id = done.get("run_id")
        if isinstance(run_id, str) and run_id:
            return Outcome(ATTESTED_UNSETTLED, None, run_id)
        return Outcome(ERROR, NO_RECEIPT)
    return Outcome(ERROR, "no_outcome: the run ended without finishing")


def judged(
    outcome: Outcome, rows: Iterable[Any], failing: Iterable[str] = ()
) -> Outcome:
    """Refine an attested firing by the settle gate's ledger rows (story 2.3).

    ``rows`` are the run's ``attested_settlements`` rows, oldest first. The
    gate judges every sealed run once, at its end (AD-12), so:

    - a ``settled`` row → ``settled``;
    - otherwise a first row ``refused`` → ``refused``, with the checks that
      failed as the detail. The gate's ``verdict_fail`` is replaced by the
      names of the signed verdicts that failed (``failing``, read from the
      run's envelope — story 2.5), so the pane reads ``refused —
      counts_match``; with none known it stays ``verdict_fail``;
    - no row → unchanged ``attested_unsettled``: no gate decision is recorded
      (the flag is off, the gate has not evaluated the run, or its row was
      not read).

    Anything but an attested firing is returned as it is.
    """
    if outcome.state != ATTESTED_UNSETTLED:
        return outcome
    row = firing_view.deciding_row(rows)
    if row is None:
        return outcome
    if getattr(row, "outcome", None) == "settled":
        return Outcome(SETTLED, None, outcome.run_id)
    if getattr(row, "outcome", None) == "refused":
        checks = firing_view.expand_checks(firing_view.gate_checks(row), failing)
        return Outcome(REFUSED, ", ".join(checks) or None, outcome.run_id)
    return outcome


def _interrupt_detail(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    capability = metadata.get("capability_id") or metadata.get("capability_name")
    raw = event.get("interrupt_type") or "interrupt"
    kind = str(getattr(raw, "value", raw))
    if capability:
        return f"awaiting approval: {capability} ({kind})"
    message = str(event.get("message") or "").strip()
    return f"awaiting the owner: {message or kind}"


def _json(value: Any) -> dict[str, Any]:
    value = getattr(value, "data", value)  # prisma.Json wraps its payload
    return dict(value) if isinstance(value, dict) else {}
