"""What is running now: a file per run, office run, ladder, routine firing and serve.

While each goes, it keeps ``<home>/live/<id>.json``: its kind and id, the
office run, ladder or firing it belongs to, its engine and model, its pid and
when that process started, when it began, and what it is doing now (a run's
latest tool call, a ladder's rung). The file goes when the work ends, however
it ends. A process killed outright leaves its file behind, and ``metabot
status`` names that entry stale: its pid is gone, has exited, or now names a
process that started at another time. A stale entry is never shown as running.

These files are UNSIGNED live state: what a process says of itself, for
``metabot status`` and the pages that show what runs. The receipt is the
record. Writing one never stops the work: a home that cannot take the file
leaves the work unlisted, never failed.

``metabot status`` also lists each routine. A routine on a workflow shows its
approval, the firings it has used and this month's money against its cap, as
its firing seals that verify count them (``metabot.workflow_firing``); a
routine with a goal of its own is listed as not approved.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import printable
from metabot.events import Started, ToolCall
from metabot.paths import kept_home, metabot_home, private_dir, safe_id, write_private

LIVE_FORMAT = "metabot.live/1"
KINDS = frozenset({"run", "office", "ladder", "routine", "serve"})
PROC = Path("/proc")  # a pid's state and start time, on Linux; elsewhere a signal 0 alone
GONE = frozenset({"Z", "X", "x"})  # exited, and not yet reaped; or dead
PID_MAX = 2**31 - 1
TEXT_LIMIT = 200
STAMP = "%Y-%m-%dT%H:%M:%SZ"
FIELDS = ("parent", "name", "engine", "model", "current")
NOT_APPROVED_LABEL = "not approved"  # as metabot.routines says it; that module imports this one

_lock = threading.Lock()  # an office run opens in a worker thread
_held: dict[str, dict[str, Any]] = {}  # what this process holds, by id


def _text(value: Any) -> str | None:
    return None if value is None else str(value)[:TEXT_LIMIT]


def _folder() -> Path:
    return kept_home() / "live"


def _write(entry: Mapping[str, Any]) -> None:
    with contextlib.suppress(OSError, ValueError):
        folder = private_dir(metabot_home() / "live")
        write_private(folder / f"{entry['id']}.json", json.dumps(entry) + "\n")


def begin_live(
    kind: str,
    live_id: str,
    *,
    parent: str | None = None,
    name: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    current: str | None = None,
) -> None:
    """Keep ``<home>/live/<live_id>.json`` until ``end_live``. It never raises."""
    try:
        safe_id(live_id, "live id")
    except ValueError:
        return
    stat = _proc_stat(os.getpid()) if _has_proc() else None
    facts = (parent, name, engine, model, current)
    entry = {
        "format": LIVE_FORMAT,
        "kind": kind,
        "id": live_id,
        **{key: _text(value) for key, value in zip(FIELDS, facts, strict=True)},
        "pid": os.getpid(),
        "proc_start": stat[1] if stat is not None else None,
        "started": utc_now(),
    }
    with _lock:
        _held[live_id] = entry
        _write(entry)


def note_live(live_id: str, **changes: Any) -> None:
    """Change what a held entry says (``current``, ``engine``, ``model`` or ``name``)."""
    with _lock:
        entry = _held.get(live_id)
        if entry is None:  # never held here, or ended
            return
        changed = {k: _text(v) for k, v in changes.items() if k in FIELDS and entry[k] != _text(v)}
        if changed:
            entry.update(changed)
            _write(entry)


def end_live(live_id: str) -> None:
    """Remove the entry this process holds. It never raises."""
    with _lock:
        if _held.pop(live_id, None) is None:
            return
        with contextlib.suppress(OSError, ValueError):
            (_folder() / f"{live_id}.json").unlink(missing_ok=True)


@contextlib.contextmanager
def hold_live(kind: str, live_id: str, **facts: Any) -> Iterator[None]:
    """Keep an entry while the block runs, and remove it when the block ends, however it ends."""
    begin_live(kind, live_id, **facts)
    try:
        yield
    finally:
        end_live(live_id)


def run_place(meta: Mapping[str, Any] | None) -> dict[str, str | None]:
    """Where a run sits, from its meta: the office run, ladder or firing that holds it, and its
    desk or rung there. The meta's own ``parent`` is the run before it, not what holds it."""
    meta = meta or {}
    if meta.get("office"):
        desk = f"desk {meta.get('desk')}" + (f".{meta['copy']}" if meta.get("copy") else "")
        if meta.get("advises"):
            desk = f"advisor {meta.get('desk')} on {meta['advises']}"
        elif meta.get("round"):
            desk += f" round {meta['round']}"
        return {"parent": str(meta["office"]), "name": desk}
    if meta.get("ladder"):
        return {"parent": str(meta["ladder"]), "name": f"rung {meta.get('rung')}"}
    if meta.get("firing"):
        return {"parent": str(meta["firing"]), "name": None}
    return {"parent": None, "name": None}


def note_event(live_id: str, event: Any) -> None:
    """A run's entry follows its events: the model its engine says it runs, its latest call."""
    if isinstance(event, ToolCall):
        note_live(live_id, current=f"step {event.tool}")
    elif isinstance(event, Started) and event.model:
        note_live(live_id, model=event.model)


# ── alive or stale ──────────────────────────────────────────────────────────


def _has_proc() -> bool:
    try:
        return (PROC / "self" / "stat").is_file()
    except OSError:  # one it cannot look at: a signal 0 tells instead
        return False


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """A process's state and start time (clock ticks after boot); None when it is not there."""
    try:
        text = (PROC / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
        fields = text.rsplit(")", 1)[1].split()  # its name, in brackets, may hold anything
        return fields[0], int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: Any, start: Any = None) -> bool:
    """Whether ``pid`` names a running process: the one that started at ``start`` when that
    is known. Never for a value that names no one process, as 0 and -1 name a group."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= PID_MAX:
        return False
    if _has_proc():
        stat = _proc_stat(pid)
        if stat is None or stat[0] in GONE:
            return False
        return start is None or (type(start) is int and stat[1] == start)
    try:
        os.kill(pid, 0)
    except PermissionError:  # there, and another user's
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _read(path: Path) -> dict[str, Any] | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("format") != LIVE_FORMAT
        or entry.get("id") != path.name[: -len(".json")]
        or entry.get("kind") not in KINDS
    ):
        return None
    return entry


def live_entries() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The entries whose process runs, then the stale ones; each list oldest first.

    An entry that does not read is stale, as ``{"id", "problem"}``.
    """
    try:
        with os.scandir(_folder()) as found:
            names = sorted(
                e.name
                for e in found
                if e.name.endswith(".json")
                and not e.name.startswith(".")  # a write cut short
                and e.is_file(follow_symlinks=False)
            )
    except OSError:  # no folder: nothing has run yet
        return [], []
    alive: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for name in names:
        entry = _read(_folder() / name)
        if entry is None:
            stale.append({"id": name[: -len(".json")], "problem": "does not read"})
        elif pid_alive(entry.get("pid"), entry.get("proc_start")):
            alive.append(entry)
        else:
            stale.append(entry)
    for found_ in (alive, stale):
        found_.sort(key=lambda e: (str(e.get("started", "")), str(e.get("id"))))
    return alive, stale


# ── metabot status ──────────────────────────────────────────────────────────


def _elapsed(started: Any, now: datetime) -> int | None:
    if not isinstance(started, str):
        return None
    try:
        began = datetime.strptime(started, STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0, int((now - began).total_seconds()))


def _span(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m{secs}s" if minutes else f"{secs}s"


def _shown(value: Any) -> str:
    return printable(str(value), TEXT_LIMIT)


def _line(entry: Mapping[str, Any], indent: str, now: datetime, alive: bool) -> str:
    if "problem" in entry:
        return f"{indent}? {_shown(entry['id'])}  {entry['problem']}"
    kind, live_id = entry.get("kind"), entry.get("id")
    parts = [f"{indent}{_shown(kind)}" + (f" {_shown(live_id)}" if live_id != kind else "")]
    if entry.get("engine"):
        model = entry.get("model")
        parts.append(_shown(entry["engine"]) + (f"/{_shown(model)}" if model else ""))
    parts += [_shown(entry[key]) for key in ("name", "current") if entry.get(key)]
    parts.append(f"pid {_shown(entry.get('pid'))}")
    started, span = entry.get("started"), _elapsed(entry.get("started"), now)
    running = f", running {_span(span)}" if alive and span is not None else ""
    parts.append(f"started {_shown(started)}{running}")
    return "  ".join(parts)


def _tree(entries: list[dict[str, Any]], now: datetime) -> list[str]:
    """Each entry, with what it holds indented under it: a desk run under its office run."""
    ids = {e["id"] for e in entries}
    under: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        parent = entry.get("parent")
        if isinstance(parent, str) and parent in ids and parent != entry["id"]:
            under.setdefault(parent, []).append(entry)
    lines: list[str] = []
    shown: set[str] = set()

    def walk(entry: dict[str, Any], depth: int) -> None:
        shown.add(entry["id"])
        lines.append(_line(entry, "  " * (depth + 1), now, alive=True))
        for child in under.get(entry["id"], []):
            if child["id"] not in shown:
                walk(child, min(depth + 1, 8))

    for entry in entries:  # the roots first; then any a loop of parents left out
        if entry["id"] not in shown and entry.get("parent") not in ids:
            walk(entry, 0)
    for entry in entries:
        if entry["id"] not in shown:
            walk(entry, 0)
    return lines


def _routines(now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """The routines on a workflow with what their ceilings counted, then the routines that
    are not approved; or why the routines do not read. It never raises."""
    from metabot.routines import routines
    from metabot.workflow_firing import next_text, summarize

    try:
        records = routines()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [], [], f"cannot read the routines: {printable(str(exc), 300)}"
    scheduled: list[dict[str, Any]] = []
    plain: list[dict[str, Any]] = []
    for record in records:
        try:
            summary = summarize(record, now)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            scheduled.append({"name": record.get("name"), "problem": str(exc)[:TEXT_LIMIT]})
            continue
        row: dict[str, Any] = {
            "name": record.get("name"),
            "id": record.get("id"),
            "on": record.get("enabled") is True,
            "next": next_text(record, now),
            "label": summary.label,
        }
        if summary.wf is None and "workflow" not in record:
            plain.append(row)
            continue
        wf, state = summary.wf, summary.state
        ceiling = wf.ceiling if wf is not None else None
        row.update(
            workflow=record.get("workflow"),
            approval=summary.approval,
            firings_used=state.used if state else None,
            firings_max=ceiling.firings if ceiling else None,
            month=state.month if state else None,
            month_spent_musd=state.spent_musd if state else None,
            month_cap_musd=ceiling.month_cap_musd if ceiling else None,
            expires=ceiling.expires if ceiling else None,
            problem=summary.problem,
        )
        scheduled.append(row)
    return scheduled, plain, None


def _scheduled_line(row: Mapping[str, Any]) -> str:
    from metabot.spend import dollars

    if "label" not in row:
        return f"  {_shown(row.get('name'))}  {_shown(row.get('problem'))}"
    parts = [_shown(row["name"]), "on" if row["on"] else "off", _shown(row["label"])]
    if row.get("firings_used") is not None:
        most = row.get("firings_max")
        parts.append(f"firings {row['firings_used']}" + (f" of {most}" if most else ""))
        spent = row.get("month_spent_musd")
        money = "not known" if spent is None else dollars(spent)
        cap = row.get("month_cap_musd")
        parts.append(f"{money}" + (f" of {dollars(cap)}" if cap else "") + f" in {row['month']}")
    if row.get("expires"):
        parts.append(f"until {_shown(row['expires'])}")
    parts.append(f"next {_shown(row['next'])}")
    if row.get("problem"):
        parts.append(f"next firing refused: {_shown(row['problem'])}")
    return "  " + "  ".join(parts)


def cmd_status(args: argparse.Namespace) -> int:
    alive, stale = live_entries()
    now = datetime.now(UTC)
    scheduled, plain, unread = _routines(now)
    if args.json:
        timed = {
            "alive": [{**e, "elapsed_s": _elapsed(e.get("started"), now)} for e in alive],
            "stale": [{**e, "elapsed_s": _elapsed(e.get("started"), now)} for e in stale],
            "scheduled": scheduled,
            "not_approved": plain,
            **({"routines_problem": unread} if unread else {}),
        }
        print(json.dumps(timed))
        return 0
    if alive:
        print("alive now (unsigned: what each process says of itself)")
        for line in _tree(alive, now):
            print(line)
    else:
        print("nothing is running")
    if stale:
        print("stale: its process is gone, so it is not running")
        for entry in stale:
            print(_line(entry, "  ", now, alive=False))
    if scheduled:
        print("scheduled workflows (counted from receipts that verify)")
        for row in scheduled:
            print(_scheduled_line(row))
    if plain:
        print("routines not approved (a goal of their own, never approved as a workflow)")
        for row in plain:
            on = "on" if row["on"] else "off"
            print(
                f"  {_shown(row['name'])}  {on}  {NOT_APPROVED_LABEL}  next {_shown(row['next'])}"
            )
    if unread:
        print(unread)
    print("next: metabot receipts show ID" if alive or stale else "next: metabot receipts list")
    return 0
