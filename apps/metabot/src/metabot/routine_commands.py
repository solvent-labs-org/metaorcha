"""``metabot routine``: save a routine, list them, fire one now, read the firings."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from metabot.approvals import printable, terminal_safe
from metabot.commands import (
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    cancelled_result,
    default_engine,
    read_prompt,
)
from metabot.connections import ConnectionStore
from metabot.console import Trace
from metabot.cron import CronError, local_zone_name
from metabot.paths import run_path
from metabot.plugin_commands import _ask
from metabot.receipts import answer_caveat, declined_and_failed, verify_file
from metabot.routines import (
    DEFAULT_TURNS,
    RoutineError,
    RoutineSpec,
    fire,
    firings,
    get_routine,
    next_due,
    remove_routine,
    routines,
    save_routine,
    set_enabled,
)


def _err(text: str) -> None:
    print(f"metabot: {text}", file=sys.stderr)


def _when(due: datetime) -> str:
    return due.strftime("%a %Y-%m-%d %H:%M %Z")


def _next(record: dict[str, Any]) -> str:
    try:
        return _when(next_due(record, datetime.now(UTC)))
    except (CronError, KeyError, TypeError, ValueError):
        return "-"


def _allowed_lines(record: dict[str, Any]) -> list[str]:
    names = {c.id: c.name for c in ConnectionStore().list()}
    lines = []
    for tool, cls in sorted((record.get("allows") or {}).items()):
        did, _, capability = tool.partition("#")
        lines.append(f"{names.get(did, did)}__{capability} ({cls})")
    return lines


def _criteria(text: str | None) -> dict[str, bool]:
    return {k.strip(): True for k in (text or "").split(",") if k.strip()}


def cmd_routine_save(args: argparse.Namespace) -> int:
    goal = read_prompt(args.goal)
    spec = RoutineSpec(
        name=args.name,
        goal=goal,
        engine=args.engine or default_engine(),
        model=args.model,
        cwd=str(Path(args.cwd or os.getcwd()).expanduser().resolve()),
        connections=list(dict.fromkeys(args.connection)),
        scope_allow=list(dict.fromkeys(args.allow)),
        criteria=_criteria(args.criteria),
        cron=args.cron,
        tz=args.tz or local_zone_name(),
        enabled=not args.off,
        turns=DEFAULT_TURNS if args.turns is None else args.turns,
        budget=args.budget,
        plugins=not args.no_plugins,
    )
    try:
        existing = get_routine(args.name)["id"]
    except KeyError:
        existing = None
    try:
        rid = save_routine(spec, routine_id=existing)
    except RoutineError as exc:
        _err(f"routine not saved: {printable(str(exc), 600)}")
        return EXIT_USAGE
    record = get_routine(rid)
    done = "updated" if existing else "saved"
    state = "on" if record["enabled"] else "off"
    print(f"{done} routine {record['name']} ({rid}), {state}; next due {_next(record)}")
    allowed = _allowed_lines(record)
    if allowed:
        print(f"  runs without asking: {', '.join(allowed)}")
    print("  any other write is declined, and a destructive call always is: nobody is there to ask")
    return EXIT_OK


def _last(rid: str, latest: dict[str, Any]) -> str:
    firing = latest.get(rid)
    return f"{firing.state} {firing.at}" if firing else "never fired"


def cmd_routine_list(_args: argparse.Namespace) -> int:
    saved = routines()
    if not saved:
        print("no routines; save one with: metabot routine save NAME --cron '0 9 * * 1-5' GOAL")
        return EXIT_OK
    latest = {f.routine_id: f for f in firings()}
    for record in saved:
        state = "on" if record.get("enabled") else "off"
        when = f"{record.get('cron')} ({record.get('tz')})"
        print(
            f"{printable(str(record.get('name')), 64):<20} {state:<4} {printable(when, 60):<36} "
            f"next {_next(record):<26} last {_last(str(record.get('id')), latest)}"
        )
    return EXIT_OK


def _toggle(args: argparse.Namespace, enabled: bool) -> int:
    try:
        record = set_enabled(args.name, enabled)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    print(f"routine {record['name']} is {'on' if enabled else 'off'}")
    return EXIT_OK


def cmd_routine_enable(args: argparse.Namespace) -> int:
    return _toggle(args, True)


def cmd_routine_disable(args: argparse.Namespace) -> int:
    return _toggle(args, False)


def cmd_routine_remove(args: argparse.Namespace) -> int:
    try:
        record = get_routine(args.name)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    if not args.yes and not sys.stdin.isatty():
        _err("remove asks first; run it in a terminal, or pass --yes")
        return EXIT_USAGE
    if not _ask(f"remove routine {record['name']}? Its firings and receipts stay.", args.yes):
        return EXIT_FAILED
    remove_routine(record["id"])
    print(f"removed routine {record['name']}")
    return EXIT_OK


def cmd_routine_firings(args: argparse.Namespace) -> int:
    rid = None
    if args.name:
        try:
            rid = get_routine(args.name)["id"]
        except KeyError as exc:
            _err(str(exc.args[0]))
            return EXIT_USAGE
    names = {r.get("id"): r.get("name") for r in routines()}
    rows = firings(rid)[-args.limit :] if args.limit > 0 else []
    if not rows:
        print("no firings yet")
        return EXIT_OK
    for f in rows:
        name = printable(str(names.get(f.routine_id, f.routine_id)), 40)
        due = f"due {f.due}" if f.due else "asked for"
        extra = [
            f"run {f.run_id}" if f.run_id else "",
            f"acceptance {f.acceptance}" if f.acceptance else "",
        ]
        reason = f"  ({printable(f.reason, 200)})" if f.reason else ""
        print(
            f"{f.at}  {name:<20} {f.state:<8} {due:<26} {' '.join(e for e in extra if e)}{reason}"
        )
    return EXIT_OK


def _journal(run_id: str | None) -> dict[str, Any]:
    """A firing's journal, or nothing when it does not read as one."""
    if not run_id:
        return {}
    try:
        journal = json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return journal if isinstance(journal, dict) else {}


def _final(journal: dict[str, Any]) -> str:
    meta = journal.get("meta")
    return str(meta.get("final") or "") if isinstance(meta, dict) else ""


def _caveat(journal: dict[str, Any]) -> str | None:
    steps, verdicts = journal.get("steps"), journal.get("verdicts")
    lists = [x if isinstance(x, list) else [] for x in (steps, verdicts)]
    return answer_caveat(*declined_and_failed(*lists))


def cmd_routine_run(args: argparse.Namespace) -> int:
    from metabot.runner import RunCancelled

    try:
        rid = get_routine(args.name)["id"]
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    trace = Trace(quiet=args.quiet)
    try:
        firing = asyncio.run(fire(rid, on_event=trace))
    except (RunCancelled, KeyboardInterrupt) as exc:
        cancelled = cancelled_result(exc)
        tail = f"; receipt {cancelled.receipt}" if cancelled is not None else ""
        trace.line(f"[metabot] stopped{tail}")
        return EXIT_CANCELLED
    if firing.state == "refused":
        _err(f"routine not fired: {printable(firing.reason or '', 400)}")
        return EXIT_FAILED
    journal = _journal(firing.run_id)
    answer = _final(journal)
    caveat = _caveat(journal)
    if answer:
        print(terminal_safe(answer) if sys.stdout.isatty() else answer, flush=True)
        if caveat:  # about the answer itself, so said even when quiet
            trace.line(f"[metabot] {caveat}")
    valid = bool(firing.receipt and verify_file(firing.receipt).valid)
    if firing.receipt:
        state = "valid" if valid else "INVALID"
        said = f"firing {firing.firing_id} {firing.state}; receipt {firing.receipt} ({state})"
        trace.note(f"[metabot] {said}")
    if firing.reason:
        trace.line(f"[metabot] {printable(firing.reason, 400)}")
    if firing.acceptance:
        trace.note(f"[metabot] declared acceptance: {firing.acceptance}")
    ok = firing.state == "sealed" and firing.reason is None and firing.acceptance != "fail"
    return EXIT_OK if ok and valid else EXIT_FAILED
