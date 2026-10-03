"""What a routine firing says, without I/O (stories 2.5 and 2.6, AD-12, AD-22).

One set of rules serves the scheduler (naming a refused firing's check), the
Gateway (the routines pane) and the session export, so the three cannot
drift apart. Every function is pure and total: malformed input degrades to
the plainest true statement, never to a positive claim.

- The **state** is the ``routine_firings`` row's, read and never derived;
  this module only words it.
- **"verified" is never said.** "checked: …" is said only where a declared
  criterion's verdict is present in the signed envelope and compared
  something.
- A run **no gate evaluated** is "attested but unsettled", and its note says
  no settle or refuse decision is recorded — never that nothing was charged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# AD-22 state → the words the pane and the export show for it.
STATE_LABELS = {
    "scheduled": "scheduled",
    "running": "running",
    "attested_unsettled": "attested but unsettled",  # FR-29, story 2.6
    "settled": "settled",
    "refused": "refused",
    "paused": "paused",
    "skipped": "skipped",
    "error": "error",
}
REFUSED_UNNAMED = "check not recorded"
UNSETTLED_NOTE = "no settlement decision is recorded for this run"
VERDICT_ONLY = "verdict only, nothing charged"  # AD-12: a ledger row with call_id NULL
UNCHECKED = "recorded, unchecked"

# The gate's failed-check id for a failing signed verdict
# (== pricing.settle_gate.CHECK_VERDICT_FAIL; equality pinned by a test).
VERDICT_FAIL = "verdict_fail"

# A declared criterion → the signed verdict that proves it was evaluated.
#   citations_required → declared_acceptance (validator/run_observer.py)
#   counts_match       → counts_match        (validator/counts_match.py)
VERDICT_FOR_CRITERION = {
    "citations_required": "declared_acceptance",
    "counts_match": "counts_match",
}
# Verdict details that mean nothing was compared, though the verdict is a
# fail. Mirrors validator/counts_match.py (_NO_OPERANDS, _NOT_EVALUATED, and
# unreadable(<NEVER_CALLED | NO_PATH>)); pinned by
# services/validator/tests/test_firing_view_contract.py.
NOT_EVALUATED_DETAILS = {
    "counts_match": ("no operands declared", "counts_match could not be evaluated"),
}
NOT_EVALUATED_MARKERS = {
    "counts_match": ("unreadable(never called)", "unreadable(no path declared)"),
}

# Gate checks that say the stored envelope itself failed verification
# (pricing/settle_gate.py: _VERIFIER_CHECK_ORDER, verify_error,
# run_id_mismatch). Its verdicts are then not evidence of anything, so no
# check is read from it. Pinned by a superagent test.
UNTRUSTED_ENVELOPE_CHECKS = frozenset(
    {
        "schema",
        "steps_root",
        "steps_merkle_root",
        "signature",
        "verify_error",
        "run_id_mismatch",
    }
)

# What the export says about a sealed firing's settlement (story 2.6).
STATEMENT_UNSETTLED = (
    "This run was sealed as a signed receipt. No settle or refuse decision is "
    "recorded for it."
)
STATEMENT_VERDICT_ONLY = (
    "The settlement gate evaluated this run. No call in it carried a charge, so "
    "its decision moved no money."
)
STATEMENT_CHARGED = "The settlement gate evaluated this run and its charged call."
STATEMENT_ROW_UNREAD = (
    "The firing records a gate decision; its ledger row could not be read."
)


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _data(value: Any) -> Any:
    return getattr(value, "data", value)  # prisma.Json wraps its payload


def deciding_row(rows: Iterable[Any] | None) -> Any | None:
    """The ledger row that decides a run: any ``settled`` row, else the oldest.

    ``rows`` are the run's ``attested_settlements`` rows, oldest first.
    """
    rows = list(rows or [])
    for row in rows:
        if _field(row, "outcome") == "settled":
            return row
    return rows[0] if rows else None


def gate_kind(row: Any) -> str | None:
    """``verdict_only`` (call_id NULL), ``charged``, or None with no row."""
    if row is None:
        return None
    return "verdict_only" if _field(row, "call_id") is None else "charged"


def gate_checks(row: Any) -> list[str]:
    """The gate's failed-check ids on a ledger row, as strings."""
    raw = _data(_field(row, "failed_checks")) if row is not None else None
    return [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []


def envelope_untrusted(row: Any) -> bool:
    """True when the gate refused the run because its envelope failed verification."""
    return bool(set(gate_checks(row)) & UNTRUSTED_ENVELOPE_CHECKS)


def failing_verdicts(envelope: Any) -> list[dict[str, str]]:
    """``[{"check", "detail"?}]`` for each failing signed verdict, envelope order.

    Deduplicated by check; a malformed or missing envelope gives ``[]``.
    """
    envelope = _data(envelope)
    verdicts = envelope.get("verdicts") if isinstance(envelope, dict) else None
    if not isinstance(verdicts, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict) or verdict.get("result") != "fail":
            continue
        check = verdict.get("check")
        if not isinstance(check, str) or not check or check in seen:
            continue
        seen.add(check)
        entry = {"check": check}
        if isinstance(verdict.get("detail"), str):
            entry["detail"] = verdict["detail"]
        out.append(entry)
    return out


def expand_checks(checks: Iterable[str], failing_names: Iterable[str]) -> list[str]:
    """The gate's checks with ``verdict_fail`` replaced by the failing verdicts' names.

    Kept as ``verdict_fail`` when no failing verdict is known. Order kept,
    duplicates dropped.
    """
    names = [n for n in failing_names if isinstance(n, str) and n]
    out: list[str] = []
    for check in checks:
        for name in names if check == VERDICT_FAIL and names else [check]:
            if name not in out:
                out.append(name)
    return out


def declared(criteria: Any) -> list[str]:
    """Criteria keys the routine declared (value ``True``), sorted.

    ``{"citations_required": False}`` passes save but declares nothing.
    """
    criteria = _data(criteria)
    if not isinstance(criteria, dict):
        return []
    return sorted(k for k, v in criteria.items() if isinstance(k, str) and v is True)


def evaluated(criterion: str, envelope: Any) -> bool:
    """True when the envelope signs a verdict for ``criterion`` that compared something."""
    check = VERDICT_FOR_CRITERION.get(criterion)
    envelope = _data(envelope)
    verdicts = envelope.get("verdicts") if isinstance(envelope, dict) else None
    if check is None or not isinstance(verdicts, list):
        return False
    details = NOT_EVALUATED_DETAILS.get(criterion, ())
    markers = NOT_EVALUATED_MARKERS.get(criterion, ())
    for verdict in verdicts:
        if not isinstance(verdict, dict) or verdict.get("check") != check:
            continue
        detail = verdict.get("detail")
        detail = detail if isinstance(detail, str) else ""
        if detail in details or any(m in detail for m in markers):
            continue
        return True
    return False


def checks_view(
    criteria: Any, envelope: Any, untrusted: bool = False
) -> tuple[str | None, str | None]:
    """``(kind, label)`` for what the run's declared checks amount to.

    - no envelope, or one the gate found ``untrusted`` (its verification
      failed) → ``(None, None)``: nothing trustworthy to read;
    - nothing declared → ``("unchecked", "recorded, unchecked")``;
    - every declared criterion evaluated → ``("checked", "checked: a, b")``;
    - otherwise → ``("not_evaluated", "declared, not evaluated: <names>")``.
    """
    if untrusted or _data(envelope) is None:
        return None, None
    names = declared(criteria)
    if not names:
        return "unchecked", UNCHECKED
    missing = [n for n in names if not evaluated(n, envelope)]
    if missing:
        return "not_evaluated", "declared, not evaluated: " + ", ".join(missing)
    return "checked", "checked: " + ", ".join(names)


def _state(state: Any) -> Any:
    return getattr(state, "value", state)  # a Prisma enum member carries .value


def state_label(state: Any, detail: Any = None) -> str:
    """The state in words; a refused firing always names its check."""
    state = _state(state)
    if state == "refused":
        named = (
            detail if isinstance(detail, str) and detail.strip() else REFUSED_UNNAMED
        )
        return f"refused — {named}"
    if isinstance(state, str):
        return STATE_LABELS.get(state, state)
    return str(state)


def note(state: Any, detail: Any, ledger_seen: bool | None = None) -> str | None:
    """The line under the label.

    - ``error`` / ``skipped`` / ``paused`` → the row's detail;
    - ``attested_unsettled`` → ``UNSETTLED_NOTE`` only when the ledger was
      read and holds no row for the run (``ledger_seen is False``): a row
      there means the firing is not yet reconciled, and a failed read proves
      nothing;
    - anything else → None.
    """
    state = _state(state)
    if state in ("error", "skipped", "paused"):
        return detail if isinstance(detail, str) and detail else None
    if state == "attested_unsettled" and ledger_seen is False:
        return UNSETTLED_NOTE
    return None
