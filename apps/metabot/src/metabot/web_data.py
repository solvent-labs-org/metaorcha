"""What ``metabot ui``'s pages show, read from the MetaBot home: plain data, never HTML.

Each reader takes what it can and says what it could not. A file that does
not read, or a record with the wrong types, becomes a line of text on the
page, never an error page. Nothing here writes a file; like every command,
a read makes the MetaBot home and its receipts folder when they are not there.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

ROW_LIMIT = 200  # runs, and firings, on one page
STEP_LIMIT = 500  # steps of one run on its page
APPROVAL = "scope_approval:"  # a person's decision on one call
ALLOWED = "scope_allow:"  # a write the run's policy let through


def is_safe_id(value: Any) -> bool:
    from metabot.paths import safe_id

    try:
        safe_id(value)
    except ValueError:
        return False
    return True


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _items(value: Any) -> list[dict[str, Any]]:
    """The mappings in a list; nothing when it is not a list."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def receipt_state(run_id: Any) -> str:
    """Whether a run's receipt verifies and its journal matches it, as the CLI says it.

    The receipt is read from where MetaBot keeps it. A path a record names is
    never opened: a hand-edited one could name any file, such as a pipe that
    never ends.
    """
    from metabot.office_run import receipt_state as state
    from metabot.paths import receipts_dir
    from metabot.seals import is_seal, seal_state

    if not is_safe_id(run_id):
        return "INVALID"
    kept = receipts_dir() / f"{run_id}.json"
    try:
        said = state(run_id, str(kept) if kept.is_file() else None)
        if said == "valid" and is_seal(json.loads(kept.read_text(encoding="utf-8"))):
            return seal_state(kept, run_id)  # a ladder's or office run's: its members too
        return said
    except (ValueError, RecursionError):
        return "INVALID"
    except OSError:
        return "cannot read"


# ── runs ────────────────────────────────────────────────────────────────────


def run_rows() -> tuple[list[dict[str, Any]], int, str | None]:
    """The newest runs in the index, each with its receipt's state; how many there are."""
    from metabot.receipts import list_index

    try:
        rows = list_index()
    except (OSError, ValueError) as exc:
        return [], 0, f"cannot read the run index: {exc}"
    newest = [dict(row) for row in reversed(rows[-ROW_LIMIT:])]
    for row in newest:
        row["state"] = receipt_state(row.get("run_id"))
    return newest, len(rows), None


def _approval(verdict: dict[str, Any]) -> str:
    result, detail = verdict.get("result"), str(verdict.get("detail") or "")
    if str(verdict.get("check")).startswith(ALLOWED) and result == "pass":
        allow = "every write" if detail == "allow_write" else detail.removeprefix("allows ")
        return f"allowed in advance: {allow}" if allow else "allowed in advance"
    if result == "pass":
        return f"approved by {detail}" if detail else "approved"
    if result == "warn":
        return detail or "declined"
    return f"{result}: {detail}"


def run_view(run_id: str) -> dict[str, Any] | None:
    """A run's journal as its page shows it; None when there is no such run.

    ``run_id`` is a safe id: the caller checked it.
    """
    from metabot.paths import run_path
    from metabot.receipts import answer_caveat, content_pins, declined_and_failed

    path = run_path(run_id) / "journal.json"
    if not path.is_file():
        return None
    view: dict[str, Any] = {"run_id": run_id, "error": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        data, view["error"] = {}, f"cannot read this run's journal: {exc}"
    if not isinstance(data, dict):
        data, view["error"] = {}, "cannot read this run's journal: it holds no object"
    meta = data.get("meta")
    view["meta"] = meta if isinstance(meta, dict) else {}
    verdicts = _items(data.get("verdicts"))
    approvals: dict[str, str] = {}
    for v in verdicts:
        check = str(v.get("check"))
        for prefix in (APPROVAL, ALLOWED):
            if check.startswith(prefix):
                approvals[check[len(prefix) :]] = _approval(v)
    steps = _items(data.get("steps"))
    view["more_steps"] = max(0, len(steps) - STEP_LIMIT)
    view["steps"] = [
        {**step, "approval": approvals.get(str(step.get("call_id")), "-")}
        for step in steps[:STEP_LIMIT]
    ]
    view["verdicts"] = verdicts
    view["caveat"] = answer_caveat(*declined_and_failed(steps, verdicts))  # every step counts
    view["receipt"] = _text(data.get("receipt"))
    view["state"] = receipt_state(run_id)
    if view["state"] == "valid" and any(v.get("check") == "seal" for v in verdicts):
        from metabot.paths import receipt_path
        from metabot.seals import seal_state

        view["state"] = seal_state(receipt_path(run_id), run_id)  # its members must hold too
    # what the receipt commits to, read only when the journal matches it, so these are signed
    good = view["state"] == "valid" or view["state"].startswith("valid, ")
    view["commits"] = sorted(content_pins(verdicts) or {}) if good else []
    for key in ("signer", "started_at", "finished_at"):
        view[key] = _text(data.get(key))
    return view


# ── an office run ───────────────────────────────────────────────────────────


def _label(row: dict[str, Any]) -> str:
    label = row.get("label")
    if isinstance(label, str):
        return label
    copy = row.get("copy")  # a record from before labels were kept
    return f"{row.get('desk')}.{copy}" if isinstance(copy, int) else str(row.get("desk"))


def office_view(office_run_id: str) -> dict[str, Any] | None:
    """An office run's record as its page shows it; None when there is no such run.

    ``office_run_id`` is a safe id: the caller checked it.
    """
    from metabot.checks import CHECKS_SUFFIX
    from metabot.office_run import load_record, office_journal_path

    if not office_journal_path(office_run_id).is_file():
        return None
    record = load_record(office_run_id)
    if record is None:
        return {
            "office_run_id": office_run_id,
            "error": "cannot read this office run's record",
            "desks": [],
        }
    view = {key: value for key, value in record.items() if key != "desks"}
    view["office_run_id"], view["error"] = office_run_id, None
    if isinstance(record.get("seal"), str):  # judged where MetaBot keeps it, by this run's id
        from metabot.paths import receipt_path
        from metabot.seals import seal_state

        view["seal_state"] = seal_state(receipt_path(office_run_id), office_run_id)
    elif record.get("seal_error"):
        view["seal_state"] = f"none: {record['seal_error']}"
    desks = []
    for raw in _items(record.get("desks")):
        row = dict(raw)
        run_id = str(row.get("run_id") or "")
        row["label"] = _label(row)
        later = type(row.get("round")) is int and row["round"] > 1
        if type(row.get("round")) is int:
            row["label"] += f" r{row['round']}"
        row["indent"] = isinstance(row.get("copy"), int) or row.get("advises") is not None or later
        row["state"] = receipt_state(run_id)
        if row.get("checks") is not None or row.get("checks_receipt") is not None:
            row["checks_state"] = receipt_state(run_id + CHECKS_SUFFIX)
        stat = row.get("stat")
        if isinstance(stat, list) and len(stat) == 3 and all(type(n) is int for n in stat):
            row["stat"] = tuple(stat)
        else:
            row["stat"] = None
        desks.append(row)
    view["desks"] = desks
    offer = record.get("offer")
    view["offer"] = [x for x in offer if isinstance(x, str)] if isinstance(offer, list) else None
    return view


# ── connections ─────────────────────────────────────────────────────────────


def _token(conn: Any, held: bool) -> str:
    """How a connection's token reaches its server: never the token itself."""
    if not held:
        return "none"
    if isinstance(conn.token_env, str) and conn.token_env:
        return f"kept by MetaBot, given as {conn.token_env}"
    if isinstance(conn.header, str) and conn.header:
        return f"kept by MetaBot, sent as {conn.header}"
    return "kept by MetaBot"


def connection_view() -> tuple[list[dict[str, Any]], str | None]:
    """Every connection: what it runs and how its token reaches it."""
    from metabot.connections import ConnectionFileError, ConnectionStore
    from metabot.paths import metabot_home

    try:
        conns = ConnectionStore().list()
    except ConnectionFileError as exc:
        return [], str(exc)
    rows = []
    for conn in conns:
        runs = " ".join(conn.command) if conn.command is not None else conn.url
        try:
            held = (metabot_home() / "secrets" / conn.suffix).is_file()  # whether, never what
        except OSError:
            held = False
        rows.append(
            {
                "name": conn.name,
                "state": conn.state,
                "transport": conn.transport,
                "id": conn.id,
                "runs": runs,
                "token": _token(conn, held),
                "origins": list(conn.origins) if isinstance(conn.origins, list) else None,
            }
        )
    return rows, None


# ── routines ────────────────────────────────────────────────────────────────


def _due_text(when: datetime) -> str:
    return when.strftime("%a %Y-%m-%d %H:%M %Z")


def routine_view(
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Every routine with when it is next due and how it last fired; the newest firings."""
    from metabot.cron import CronError
    from metabot.paths import metabot_home
    from metabot.routines import ROUTINES, firings, next_due, routines

    now = now or datetime.now(UTC)
    try:
        records = routines()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [], [], f"cannot read {metabot_home() / ROUTINES}: {exc}"
    try:
        fired = firings()
    except (OSError, ValueError) as exc:
        return [], [], f"cannot read the firings: {exc}"
    last = {f.routine_id: f for f in fired}
    names = {str(r.get("id")): r.get("name") for r in records}
    rows = []
    for record in records:
        try:
            due = _due_text(next_due(record, now))
        except (AttributeError, CronError, KeyError, TypeError, ValueError):
            due = "-"
        rows.append({**record, "next_due": due, "last": last.get(str(record.get("id")))})
    recent = [
        {**asdict(f), "routine_name": names.get(f.routine_id, f.routine_id)}
        for f in reversed(fired[-ROW_LIMIT:])
    ]
    return rows, recent, None
