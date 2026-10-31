"""``metabot office``: a Code Desks office, run headless."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from metabot.approvals import printable
from metabot.config import Config, load_config
from metabot.desks import BRANCH_ROOT, UNDO, DeskGitError, clean, list_desks, pick
from metabot.gitcmd import GitError, toplevel
from metabot.office import (
    OfficeError,
    OfficeSpec,
    cwd_link_problem,
    desk_problem,
    desk_runs_on,
    find_office,
    load_office,
    office_root,
)


def _s(value: object, limit: int) -> str:
    return printable(str(value), limit)


def _err(text: str) -> None:
    print(_s(text, 2000), file=sys.stderr, flush=True)


def _lines_err(text: str) -> None:
    """A message whose lines were each made safe to show, such as a checks refusal."""
    for line in text.splitlines():
        _err(line)


def office_from_args(args: argparse.Namespace) -> OfficeSpec | None:
    """The office ``--dir`` names, else the one here or above; None after saying why."""
    from metabot.cmdline import given_folder

    cfg = load_config()
    if getattr(args, "dir", None):
        folder, usable = given_folder(args.dir)
        if not usable:
            _err(f"metabot: {folder} is not a directory")
            return None
        root: Path | None = office_root(folder)
    else:
        try:
            root = find_office()
        except OSError as exc:  # no office pinned, and the current folder was removed
            _err(f"metabot: {_no_folder(exc)}")
            return None
    if root is None:
        _err("metabot: no office.yaml here or above; Code Desks' `desk init` writes one")
        return None
    try:
        return load_office(root, cfg.fan_cap)
    except OfficeError as exc:
        _err(f"metabot: {root / 'office.yaml'}: {exc}")
        return None


def roster_of(cfg: Config) -> Mapping[str, Mapping[str, str]]:
    """The roster as it stands here: the person's roles, and the defaults for the rest."""
    from metabot.roster import roster_entries

    return roster_entries(cfg)


def desk_line(desk, roster) -> str:
    parts = [
        f"{_s(desk.name, 32):<12}",
        f"{_s(desk_runs_on(desk, roster), 60):<24}",
        f"budget {desk.budget if desk.budget is not None else '-'}",
        f"fan {desk.fan}",
        f"turns {desk.turns}",
    ]
    if desk.kit:
        parts.append("kit " + _s(",".join(desk.kit), 120))
    if desk.advises:
        parts.append("advises " + _s(",".join(desk.advises), 120))
    return "  ".join(parts)


def _cwd_link(office: OfficeSpec) -> None:
    """A cwd that is a link out of the worktree is named here, before a run."""
    for desk in office.desks.values():
        problem = cwd_link_problem(office, desk)
        if problem:
            raise OfficeError(f"desks.{desk.name}.cwd", problem)


def cmd_office_check(args: argparse.Namespace) -> int:
    from metabot.kits import kit_problem

    office = office_from_args(args)
    if office is None:
        return 2
    try:
        _cwd_link(office)
    except OfficeError as exc:
        _err(f"metabot: {office.root / 'office.yaml'}: {exc}")
        return 2
    roster = roster_of(load_config())
    print(f"office  {_s(office.name, 80)}  {_s(office.root, 300)}")
    print("desks")
    blocked: list[tuple[str, str]] = []
    for desk in office.desks.values():
        print("  " + desk_line(desk, roster))
        problem = desk_problem(desk, roster) or kit_problem(office, desk)
        if problem:
            blocked.append((desk.name, problem))
    print("checks")
    checks = office.checks()
    for key, command in checks.items():
        print(f"  {_s(key, 80):<24}  {_s(command, 300)}")
    if not checks:
        print("  none")
    if blocked:
        print("cannot run here")
        for name, problem in blocked:
            print(f"  ! {_s(name, 32)}: {_s(problem, 400)}")
    return 0


def _no_folder(exc: OSError) -> str:
    return f"the current folder cannot be read ({exc.strerror or exc}); give --dir"


def _repo(args: argparse.Namespace) -> Path | None:
    try:
        start = Path(args.dir) if getattr(args, "dir", None) else Path.cwd()
    except OSError as exc:  # the current folder was removed
        _err(f"metabot: {_no_folder(exc)}")
        return None
    top = toplevel(start)
    if top is None:
        _err(f"metabot: {start} is not in a git repository")
    return top


def _stat(stat: tuple[int, int, int] | None) -> str:
    if stat is None:
        return "-"
    files, added, removed = stat
    return f"{files} file{'' if files == 1 else 's'} +{added} -{removed}"


def cmd_desks_list(args: argparse.Namespace) -> int:
    repo = _repo(args)
    if repo is None:
        return 2
    rows = list_desks(repo)
    if not rows:
        print("no desk branches here")
    for row in rows:
        name = _s(f"{row.run_id}/{row.label}", 200)
        print(f"{name:<40}  {row.sha[:7]}  {_stat(row.stat):<20}  {_s(row.subject, 120)}")
    return 0


def cmd_desks_pick(args: argparse.Namespace) -> int:
    repo = _repo(args)
    if repo is None:
        return 2
    prefix = f"{BRANCH_ROOT}/"
    branch = args.desk if args.desk.startswith(prefix) else prefix + args.desk
    try:
        paths = pick(repo, branch)
    except (DeskGitError, GitError) as exc:
        _err(f"metabot: {exc}")
        return 1
    if not paths:
        print(f"{_s(branch, 200)}: nothing to stage; your checkout has this work already")
        return 0
    print(f"staged from {_s(branch, 200)}:")
    for path in paths:
        print(f"  {_s(path, 300)}")
    print(
        "nothing was committed. Review with git diff --cached; git commit keeps the run id "
        f"in the message. Undo with {UNDO}."
    )
    return 0


def cmd_desks_clean(args: argparse.Namespace) -> int:
    repo = _repo(args)
    if repo is None:
        return 2
    if not args.yes and not sys.stdin.isatty():
        _err("metabot: clean asks before it removes anything; run it in a terminal, or pass --yes")
        return 2

    def confirm(items: list[str]) -> bool:
        print(f"run {_s(args.run, 130)}: these desk worktrees and branches go")
        for item in items:
            print(f"  {_s(item, 300)}")
        if args.yes:
            return True
        return input("remove them? [y/N] ").strip().lower() in ("y", "yes")

    try:
        result = clean(repo, args.run, confirm)
    except ValueError as exc:
        _err(f"metabot: {exc}")
        return 2
    except GitError as exc:
        _err(f"metabot: {exc}")
        return 1
    if not result.removed and not result.kept:
        print("nothing removed")
    for item in result.removed:
        print(f"removed  {_s(item, 300)}")
    for item, why in result.kept.items():
        print(f"kept     {_s(item, 300)}: {_s(why, 300)}")
    return 1 if result.kept else 0


def _runs_text(engine: str, model: str | None) -> str:
    return f"{engine}/{model}" if model else engine


def _said_otherwise(row: Any) -> None:
    """After a desk's answer, how many of its calls were declined or failed."""
    from metabot.receipts import run_caveat

    caveat = run_caveat(row.run_id)
    if caveat:  # about the answer itself, so said even when quiet
        print(f"[metabot] {_s(row.label, 40)}: {caveat}", file=sys.stderr, flush=True)


def cmd_office_run(args: argparse.Namespace) -> int:
    import asyncio

    from metabot.approvals import pick_approver, terminal_safe
    from metabot.cmdline import read_prompt
    from metabot.console import Trace
    from metabot.office_run import ROUNDS_DEFAULT, receipt_state, run_fan, run_relay
    from metabot.runner import RunCancelled
    from metabot.spend import dollars

    if args.rounds is not None and not args.until_checks:
        _err("metabot: --rounds goes with --until-checks")
        return 2
    rounds = (args.rounds or ROUNDS_DEFAULT) if args.until_checks else 1
    checks = args.checks or args.until_checks
    office = office_from_args(args)
    if office is None:
        return 2
    task = read_prompt(args.task)
    desks = [d.strip() for d in args.desks.split(",") if d.strip()] if args.desks else None
    trace = Trace(quiet=args.quiet)

    def on_desk(row) -> None:
        trace.note(
            f"[metabot] desk {_s(row.label, 40)}: {_s(_runs_text(row.engine, row.model), 80)}"
        )

    kw: dict[str, Any] = {} if args.fan else {"advisors": not args.no_advisors}
    runner = run_fan if args.fan else run_relay
    try:
        result = asyncio.run(
            runner(
                office,
                task,
                approver=pick_approver(),
                desks=desks,
                allow_write=args.allow_write,
                allows=args.allow,
                roster=roster_of(load_config()),
                connections=not args.no_connections,
                plugins=not args.no_plugins,
                on_event=trace,
                on_desk=on_desk,
                checks=checks,
                rounds=rounds,
                max_cost_musd=args.max_cost,
                **kw,
            )
        )
    except OfficeError as exc:
        if exc.where == "checks":  # its lines name each check, made safe in metabot.checks
            _lines_err(f"metabot: {exc}")
        else:
            _err(f"metabot: {exc}")
        return 2
    except (RunCancelled, KeyboardInterrupt):
        trace.line("[metabot] cancelled; what each desk did is on its branch")
        return 130
    except (DeskGitError, GitError, ValueError) as exc:  # a PluginError is a ValueError
        _err(f"metabot: the office run stopped: {printable(str(exc), 300)}")
        return 1
    # a run whose record is not on disk did not end well: the trace said why as it closed
    ended_well = result.ok and result.record_error is None
    print(f"office run {result.office_run_id}  {result.mode}")
    for row in result.rows:
        state = receipt_state(row.run_id, str(row.receipt) if row.receipt else None)
        label = row.label + (f" r{row.round}" if row.round else "")
        line = (
            f"  {_s(label, 40):<12} {'ok' if row.ok else 'error':<6} "
            f"{_s(_runs_text(row.engine, row.model), 60):<24} receipt {state:<8} {_stat(row.stat)}"
        )
        if checks:
            line += f"  checks {_s(row.checks or _unchecked(row), 300)}"
        if row.error:
            line += f"  {_s(row.error, 300)}"
        if row.stopped:
            line += f"  ({_s(row.stopped, 200)})"
        print(line)
    if args.max_cost is not None:
        spent = sum(row.cost_musd or 0 for row in result.rows)
        if result.cost_unknown:  # a run that counted no usage: the total is a floor
            unknown = _s(", ".join(result.cost_unknown), 200)
            print(
                f"  spent at least {dollars(spent)} of {dollars(args.max_cost)}: "
                f"the cost of {unknown} is unknown"
            )
        else:
            print(f"  spent {dollars(spent)} of {dollars(args.max_cost)}")
    for key, why in result.checks_skipped.items():
        print(f"  check {_s(key, 80)} skipped: {_s(why, 200)}")
    if checks and all(r.checks is None for r in result.rows if r.advises is None):
        print("  no check ran: no desk changed a file")
    if result.project_copies:
        where = result.project_copies[0].parent
        print(f"  project copies: {len(result.project_copies)} receipts in {_s(where, 300)}")
    for why in result.project_errors:
        print(f"  {_s(why, 300)}; the receipts in the MetaBot home count")
    if result.seal is not None:
        print(f"  sealed over every run: metabot receipts verify {result.office_run_id}")
    tty = sys.stdout.isatty()
    advice = [r for r in result.rows if r.advises is not None]
    for row in advice:  # an advisor's output is for the person, whomever it advised
        if row.answer:
            print(f"advice of {_s(row.desk, 40)} on {_s(row.advises, 40)}:")
            print(terminal_safe(row.answer) if tty else row.answer, flush=True)
            _said_otherwise(row)
    # a fan's desks all count; a relay's are the last desk's runs, one or its copies
    worked = [r for r in result.rows if r.advises is None and r.next_round is None]
    last = worked[-1].desk if worked else None
    shown = worked if result.mode == "fan" else [r for r in worked if r.desk == last]
    for row in shown:
        if row.answer:
            print(f"answer of {_s(row.label, 40)}:")
            print(terminal_safe(row.answer) if tty else row.answer, flush=True)
            _said_otherwise(row)
    if not any(row.ok for row in shown):
        return 0 if ended_well else 1  # it stopped short: its table says where
    if not result.offer:
        if any(row.commit for row in worked):  # a failed desk's, or a branch since gone
            print(
                "nothing to pick: no desk that finished left work on its branch; "
                "metabot desks list shows every desk branch"
            )
        else:
            print("nothing to pick: no desk changed a file")
    elif len(result.offer) == 1 and result.mode == "relay":
        print(
            f"next: metabot desks pick {result.office_run_id}/{result.offer[0]}"
            "  (stages the relay's work in your checkout)"
        )
    else:
        for label in result.offer:
            print(f"pick one: metabot desks pick {result.office_run_id}/{label}")
    return 0 if ended_well else 1


def cmd_office_trust(args: argparse.Namespace) -> int:
    from metabot.checks import CheckError, record_trust, refusal, trust_diff

    office = office_from_args(args)
    if office is None:
        return 2
    checks = office.checks()
    if not checks:
        print("the office has no gate checks; there is nothing to trust")
        return 0
    refused = refusal(checks)
    if refused is not None:
        _lines_err(f"metabot: {refused}")
        return 2
    try:
        diff = trust_diff(office)
    except CheckError as exc:
        _lines_err(f"metabot: {exc}")
        return 2
    if diff.empty:
        print(f"the office's {len(checks)} gate checks are trusted on this machine already")
        return 0
    print(
        "office run --checks runs these after each desk, in the desk's worktree, "
        "on the desk's own work:"
    )
    for line in diff.lines():
        print(line)  # every command whole: lines() shows each one with nothing unprintable
    print(
        "A check that runs the project, such as its tests, runs code the desks wrote, "
        "as you, with your environment."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            _err("metabot: trust asks first; run it in a terminal, or pass --yes")
            return 2
        if input("trust them on this machine? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not trusted")
            return 1
    print(f"trusted on this machine; recorded in {_s(record_trust(office), 2000)}")
    return 0


def _unchecked(row: Any) -> str:
    """Why a desk has no checks result: it changed no file, or it did not get that far."""
    if row.read_only:
        return READ_ONLY
    return NO_CHANGE if row.ok and row.advises is None else "-"


NO_CHANGE = "- (no file changed)"
READ_ONLY = "- (read-only)"


def _kept(run_id: str) -> str | None:
    """A run's receipt where MetaBot keeps it, if there is one; ValueError for no run id."""
    from metabot.paths import receipt_path

    path = receipt_path(run_id)
    return str(path) if path.is_file() else None


def show_office(record: dict, office_run_id: str) -> int:
    """An office run's tree; its seal is judged as kept under ``office_run_id``, the id the
    record was read by, never by a path the record names."""
    from metabot.checks import CHECKS_SUFFIX
    from metabot.office_run import receipt_state
    from metabot.paths import receipt_path
    from metabot.seals import seal_state

    print(
        f"office run  {_s(record.get('office_run_id'), 130)}  {_s(record.get('office'), 80)}  "
        f"{_s(record.get('mode'), 20)}"
    )
    print(f"task        {_s(record.get('task', ''), 300)}")
    started, finished = _s(record.get("started_at"), 40), _s(record.get("finished_at"), 40)
    print(f"started     {started}   finished {finished}")
    if isinstance(record.get("seal"), str):
        seal = receipt_path(office_run_id)
        print(f"seal        {_s(str(seal), 600)} ({seal_state(seal, office_run_id)})")
    elif record.get("seal_error"):
        print(f"seal        none: {_s(record.get('seal_error'), 300)}")
    for row in record.get("desks") or []:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or "")
        try:  # where MetaBot keeps it, never a path the record names
            state = receipt_state(run_id, _kept(run_id))
        except ValueError:
            state = "INVALID"
        stat = row.get("stat")
        shown_stat = _stat(tuple(stat)) if isinstance(stat, list) and len(stat) == 3 else "-"
        runs = _runs_text(str(row.get("engine") or "?"), row.get("model"))
        copy = row.get("copy")
        label = row.get("label")
        if not isinstance(label, str):  # a record from before labels were kept
            label = f"{row.get('desk')}.{copy}" if isinstance(copy, int) else row.get("desk")
        line = (
            f"  {_s(label, 40):<12} {'ok' if row.get('ok') else 'error':<6} {state:<8} "
            f"{_s(runs, 60):<24} {shown_stat:<20} {_s(run_id, 200)}"
        )
        checks, checks_receipt = row.get("checks"), row.get("checks_receipt")
        if isinstance(checks, str) or isinstance(checks_receipt, str):
            try:
                checks_state = receipt_state(run_id + CHECKS_SUFFIX, _kept(run_id + CHECKS_SUFFIX))
            except ValueError:
                checks_state = "INVALID"
            line += f"  checks {_s(checks or '-', 200)} (receipt {checks_state})"
        elif record.get("checks") and row.get("read_only") is True:
            line += f"  checks {READ_ONLY}"
        elif record.get("checks") and row.get("ok") and row.get("advises") is None:
            line += f"  checks {NO_CHANGE}"
        if row.get("error"):
            line += f"  {_s(row.get('error'), 300)}"
        print(line)
    return 0
