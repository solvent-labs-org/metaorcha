"""``metabot workflow``: make, check, show and list workflow files."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from metabot.approvals import printable, terminal_safe
from metabot.cmdline import EXIT_FAILED, EXIT_OK, EXIT_USAGE, given_folder, read_prompt
from metabot.workflow_approval import (
    NotApproved,
    approval_for_run,
    approval_state,
    approve,
    revoke,
)
from metabot.workflows import (
    FORMAT,
    MAX_BYTES,
    Step,
    Workflow,
    WorkflowError,
    ask_engine,
    here_problems,
    load_workflow,
    open_office,
    parse_workflow,
    resolve,
    store_workflow,
    stored_workflows,
    workflow_bytes,
)

FIRES_SHOWN = 5
PAD = " " * 10


def _err(text: str) -> None:
    print(f"metabot: {text}", file=sys.stderr)


def _s(value: object, limit: int = 300) -> str:
    return printable(str(value), limit)


# ── new ─────────────────────────────────────────────────────────────────────

OFFICE_ONLY = (
    ("desks", "--desks"),
    ("checks", "--checks"),
    ("until_checks", "--until-checks"),
    ("rounds", "--rounds"),
    ("fan", "--fan"),
    ("no_advisors", "--no-advisors"),
)


class FlagError(ValueError):
    pass


def _given(args: argparse.Namespace, key: str) -> bool:
    value = getattr(args, key, None)
    return value not in (None, False, [], "")


def _kind(args: argparse.Namespace) -> str:
    if args.ladder and args.office:
        raise FlagError("a step is --ladder or --office, not both")
    kind = "ladder" if args.ladder else "office" if args.office else "ask"
    if kind != "ask":
        for key, flag in (("login", "--login"), ("engine", "-e"), ("model", "-m")):
            if _given(args, key):
                raise FlagError(f"{flag} goes with an ask step")
    if args.check and kind != "ladder":
        raise FlagError("--check goes with --ladder: it judges each rung's work")
    if kind != "office":
        for key, flag in OFFICE_ONLY:
            if _given(args, key):
                raise FlagError(f"{flag} goes with --office")
    else:
        if args.role:
            raise FlagError("--role goes with an ask step, or --role private with --ladder")
        if args.read_only:
            raise FlagError("--read-only goes with an ask or a ladder step")
    return kind


def _office_step(args: argparse.Namespace) -> dict[str, Any]:
    from metabot.config import load_config
    from metabot.office import load_office, office_root

    folder, usable = given_folder(args.office)
    if not usable:
        raise FlagError(f"{folder} is not a directory")
    try:
        office = load_office(office_root(folder), load_config().fan_cap)
    except ValueError as exc:  # OfficeError: none there, or one that does not read
        raise FlagError(str(exc)) from None
    step: dict[str, Any] = {"dir": str(office.root), "office_sha256": office.sha256}
    if args.desks:
        step["desks"] = [d.strip() for d in args.desks.split(",") if d.strip()]
    if args.checks:
        step["checks"] = True
    if args.until_checks:
        step["until_checks"] = True
    if args.rounds is not None:
        step["rounds"] = args.rounds
    if args.fan:
        step["fan"] = True
    if args.no_advisors:
        step["advisors"] = False
    return step


def _data(args: argparse.Namespace, prompt: str, cwd: Path) -> dict[str, Any]:
    kind = _kind(args)
    step: dict[str, Any] = {"name": kind, "kind": kind, "prompt": prompt}
    if kind == "ask":
        for key in ("engine", "model", "role", "login"):
            if _given(args, key):
                step[key] = getattr(args, key)
    elif kind == "ladder":
        if args.check:
            step["checks"] = list(args.check)
        if args.role:
            step["role"] = args.role
    else:
        step.update(_office_step(args))
    if args.read_only:
        step["read_only"] = True
    if args.allow_write:
        step["allow_write"] = True
    if args.allow:
        step["allow"] = list(dict.fromkeys(args.allow))
    if args.max_cost is not None:
        step["max_cost"] = args.max_cost
    if args.no_connections:
        step["connections"] = False
    if args.no_plugins:
        step["plugins"] = False
    data: dict[str, Any] = {"format": FORMAT, "name": args.name, "cwd": str(cwd), "steps": [step]}
    if args.cron is not None:
        from metabot.cron import local_zone_name

        data["schedule"] = {"cron": args.cron, "tz": args.tz or local_zone_name()}
    elif args.tz:
        raise FlagError("--tz goes with --cron")
    ceiling: dict[str, Any] = {}
    if args.firings is not None:
        ceiling["firings"] = args.firings
    if args.expires is not None:
        ceiling["expires"] = args.expires
    if args.month_cap is not None:
        ceiling["month_cap"] = args.month_cap
    if ceiling:
        data["ceiling"] = ceiling
    return data


def _raw_from_args(args: argparse.Namespace) -> bytes:
    prompt = read_prompt(args.prompt)
    if not prompt.strip():
        raise FlagError("the step needs a prompt: what it asks the engine to do")
    cwd, usable = given_folder(args.cwd)
    if not usable:
        raise FlagError(f"{cwd} is not a directory")
    return workflow_bytes(_data(args, prompt, cwd))


def _newest_before(name: str) -> str | None:
    try:
        return resolve(name)
    except KeyError:
        return None


def _approval(wf: Workflow) -> tuple[str, str]:
    """Whether the workflow's bytes, as they are now, are approved here; and what is next."""
    state = approval_state(wf.sha256)
    if state.unreadable_revokes:
        ids = ", ".join(state.unreadable_revokes)
        return (
            f"taken as revoked: a revoke receipt for it does not verify ({ids})",
            f"metabot receipts verify {state.unreadable_revokes[0]}",
        )
    if state.in_force:
        newest = max(state.in_force, key=lambda a: (a.signed_at, a.run_id))
        return f"approved at {newest.signed_at} (receipt {newest.run_id})", (
            f"metabot workflow run {wf.name}"
        )
    if state.revoked:
        return "revoked", f"metabot workflow approve {wf.name}"
    return "not approved", f"metabot workflow approve {wf.name}"


def next_line(wf: Workflow) -> str:
    return f"next: metabot workflow show {wf.name}, then {_approval(wf)[1]}"


def _stored(raw: bytes) -> int:
    """Keep ``raw`` and say what was kept, what it replaced, and what stops it here."""
    try:
        before = _newest_before(parse_workflow(raw).name)
        wf = store_workflow(raw)
    except WorkflowError as exc:
        _err(f"workflow not kept: {_s(exc, 600)}")
        return EXIT_USAGE
    steps = len(wf.steps)
    print(
        f"stored workflow {wf.name} {wf.sha256} ({steps} step{'' if steps == 1 else 's'}), "
        "not approved"
    )
    if before is not None and before != wf.sha256:
        print(f"  {wf.name} now finds this version; {before[:12]} stays, by its sha256")
    for problem in here_problems(wf):
        print(f"  cannot run here yet: {_s(problem, 600)}")
    print(next_line(wf))
    return EXIT_OK


def cmd_workflow_new(args: argparse.Namespace) -> int:
    try:
        raw = _raw_from_args(args)
    except (FlagError, WorkflowError) as exc:
        _err(f"workflow not kept: {_s(exc, 600)}")
        return EXIT_USAGE
    return _stored(raw)


def cmd_workflow_add(args: argparse.Namespace) -> int:
    try:
        with open(args.file, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        _err(f"cannot read {_s(args.file)}: {exc.strerror or exc}")
        return EXIT_USAGE
    return _stored(raw)


# ── check ───────────────────────────────────────────────────────────────────


def _read(ref: str) -> tuple[Workflow, bool]:
    """The workflow ``ref`` names: a file when one is there, else a stored one; and whether
    it came from a file. KeyError, WorkflowError or OSError when it cannot be read."""
    path = Path(ref)
    if path.is_file():
        with path.open("rb") as fh:
            return parse_workflow(fh.read(MAX_BYTES + 1)), True
    return load_workflow(ref), False


def cmd_workflow_check(args: argparse.Namespace) -> int:
    try:
        wf, from_file = _read(args.ref)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    except OSError as exc:
        _err(f"cannot read {_s(args.ref)}: {exc.strerror or exc}")
        return EXIT_USAGE
    except WorkflowError as exc:
        _err(f"not a workflow: {_s(exc, 600)}")
        return EXIT_FAILED
    steps = len(wf.steps)
    print(f"workflow {wf.name} {wf.sha256} ({steps} step{'' if steps == 1 else 's'})")
    problems = here_problems(wf)
    for problem in problems:
        _err(f"cannot run here: {_s(problem, 600)}")
    if not problems:
        print("it can run here")
    print(f"next: metabot workflow add {args.ref}" if from_file else next_line(wf))
    return EXIT_FAILED if problems else EXIT_OK


# ── show ────────────────────────────────────────────────────────────────────


def _endpoint_host(base_url: str) -> str:
    """An endpoint's host and port, never a user or a password the address may hold."""
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = parsed.hostname or "?"
    return f"{host}:{port}" if port else host


def _ask_line(step: Step) -> str:
    from metabot.config import load_config

    try:
        _engine, name, model = ask_engine(step)
    except ValueError as exc:
        return f"cannot run here: {_s(exc)}"
    cfg = load_config()
    picked = name if (step.engine or step.role) else f"{name} (the default here)"
    parts = [picked + (f", role {step.role}" if step.role else "")]
    model = model or cfg.models.get(name) or (cfg.openai_model if name == "openai" else None)
    parts.append(f"model {model or 'its default'}")
    if name == "openai":
        parts.append(f"endpoint {_endpoint_host(cfg.openai_base_url)}")
    parts.append(f"login {step.login}" if step.login else "no login")
    return _s(", ".join(parts), 600)


def _ladder_lines(step: Step) -> list[str]:
    from metabot.config import load_config
    from metabot.ladder import attempts, effective_ladder

    try:
        cfg = load_config()
        rungs, whose, _note = effective_ladder(cfg)
        most = attempts(cfg)
    except ValueError as exc:
        return [f"cannot run here: {_s(exc)}"]
    said = " -> ".join(f"{r.tier} {r.label()}" for r in rungs[:most]) or "no rung here"
    lines = [f"ladder ({whose}): {_s(said, 600)}"]
    if step.role == "private":
        lines.append("private: rungs on this machine alone")
    lines += [f"check: {_s(c, 600)}" for c in step.ladder_checks]
    return lines


def _office_lines(step: Step) -> list[str]:
    try:
        office = open_office(step)
    except (ValueError, OSError) as exc:
        return [f"cannot run here: {_s(exc)}"]
    mode = "fan" if step.fan else "relay"
    desks = ", ".join(step.desks) if step.desks else "every desk but the advisors"
    lines = [
        f"office {_s(office.name, 80)} at {_s(step.dir)} (office.yaml {office.sha256[:12]}), "
        f"{mode}, desks {_s(desks)}"
    ]
    if step.office_checks:
        lines += [f"check {_s(k, 80)}: {_s(c, 600)}" for k, c in office.checks().items()]
    if step.until_checks:
        lines.append(f"until its checks pass, up to {step.rounds or 3} rounds")
    if not step.fan and not step.advisors:
        lines.append("no advisors")
    return lines


def _writes(step: Step) -> str:
    from metabot.spend import dollars

    if step.read_only:
        said = ["reads and changes nothing"]
    else:
        said = ["writes run without asking" if step.allow_write else "each write asks"]
    if step.allow:
        said.append(f"runs without asking: {', '.join(_s(a, 80) for a in step.allow)}")
    if step.max_cost_musd is not None:
        said.append(f"money cap {dollars(step.max_cost_musd)} a run")
    if not step.connections:
        said.append("no connections")
    if not step.plugins:
        said.append("no plugins")
    return "; ".join(said)


def step_lines(step: Step) -> list[str]:
    """What a step runs on and may do, as the approval screen shows it."""
    if step.kind == "ask":
        head, more = _ask_line(step), []
    elif step.kind == "ladder":
        [head, *more] = _ladder_lines(step)
    else:
        [head, *more] = _office_lines(step)
    lines = [f"{step.name} ({step.kind}): {head}", *more]
    if step.kind != "office":
        lines.append(_writes(step))
    else:
        lines.append(_writes(step).replace("each write asks", "each write asks, desk by desk"))
    lines.append(f"prompt: {_s(step.prompt, 300)}")
    return lines


def _fires(wf: Workflow) -> list[str]:
    from metabot.cron import Cron, zone

    assert wf.schedule is not None
    tz = zone(wf.schedule.tz)
    cron, after, times = Cron.parse(wf.schedule.cron), datetime.now(UTC), []
    for _ in range(FIRES_SHOWN):
        after = cron.next_after(after, tz)
        times.append(after.strftime("%a %Y-%m-%d %H:%M %Z"))
    return times


def ceiling_text(wf: Workflow) -> str:
    from metabot.spend import dollars

    c = wf.ceiling
    if c is None:
        return "none"
    said = []
    if c.firings is not None:
        said.append(f"{c.firings} firing{'' if c.firings == 1 else 's'}")
    if c.expires is not None:
        said.append(f"expires {c.expires}")
    if c.month_cap_musd is not None:
        said.append(f"{dollars(c.month_cap_musd)} a month")
    return "; ".join(said)


def fact_lines(wf: Workflow) -> list[str]:
    """The facts a person approves: every step, its checks and allows, when it fires, and
    its ceiling."""
    lines = [f"workflow  {wf.name}", f"sha256    {wf.sha256}"]
    if wf.about:
        lines.append(f"about     {_s(wf.about, 600)}")
    lines.append(f"folder    {_s(wf.cwd, 600)}")
    for n, step in enumerate(wf.steps, 1):
        head, *rest = step_lines(step)
        lines.append(f"step {n:<4} {head}")
        lines += [f"{PAD}{line}" for line in rest]
    if wf.schedule is None:
        lines.append("schedule  none: it runs when you run it")
    else:
        lines.append(f"schedule  {_s(wf.schedule.cron, 80)} ({_s(wf.schedule.tz, 80)})")
        lines.append(
            f"{PAD}serve fires it once a routine names it: metabot workflow schedule "
            f"{_s(wf.name, 64)}"
        )
        first, *rest = _fires(wf)
        lines.append(f"fires     {first}")
        lines += [f"{PAD}{when}" for when in rest]
    lines.append(f"ceiling   {ceiling_text(wf)}")
    if wf.schedule is not None:
        lines.append(f"{PAD}It runs until you change it, turn it off, or a ceiling is reached.")
        if wf.ceiling is not None and wf.ceiling.expires is not None:
            lines.append(f"{PAD}It makes no firing on or after the day it expires, in its zone.")
    return lines


def _load(ref: str) -> Workflow | int:
    try:
        return load_workflow(ref)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    except WorkflowError as exc:
        _err(_s(exc, 600))
        return EXIT_FAILED


def cmd_workflow_show(args: argparse.Namespace) -> int:
    wf = _load(args.ref)
    if isinstance(wf, int):
        return wf
    if args.raw:  # the bytes it is addressed by; they read as UTF-8, or it would not load
        sys.stdout.write(wf.raw.decode("utf-8"))
        return EXIT_OK
    for line in fact_lines(wf):
        print(line)
    said, then = _approval(wf)
    print(f"approval  {said}")
    print(f"next: {then}")
    return EXIT_OK


# ── list ────────────────────────────────────────────────────────────────────


def cmd_workflow_list(_args: argparse.Namespace) -> int:
    rows = stored_workflows()
    if not rows:
        print("no workflows; make one with: metabot workflow new NAME -e ENGINE PROMPT")
        return EXIT_OK
    for row in rows:
        try:
            wf = load_workflow(row.sha256)
            steps = f"{len(wf.steps)} step{'' if len(wf.steps) == 1 else 's'}"
            state = _approval(wf)[0].split(" (")[0].split(" at ")[0]
        except (KeyError, WorkflowError):
            steps, state = "-", "CHANGED since it was stored"
        older = "  older version" if not row.newest else ""
        print(
            f"{_s(row.name, 64):<20} {row.sha256[:12]}  {state:<14} {steps:<8} "
            f"stored {row.stored_at}{older}"
        )
    return EXIT_OK


# ── approve, revoke, run ────────────────────────────────────────────────────


def _ask_yes(question: str, yes: bool) -> bool:
    from metabot.plugin_commands import _ask

    return _ask(question, yes)


def cmd_workflow_approve(args: argparse.Namespace) -> int:
    from metabot.receipts import verify_file

    wf = _load(args.ref)
    if isinstance(wf, int):
        return wf
    state = approval_state(wf.sha256)
    if state.unreadable_revokes:
        _err(
            f"{wf.name} {wf.short}: {_approval(wf)[0]}; approving it again does not change "
            f"that: {_approval(wf)[1]}"
        )
        return EXIT_FAILED
    if state.in_force:
        print(f"{wf.name} {wf.short} is already {_approval(wf)[0]}")
        print(f"next: metabot workflow run {wf.name}")
        return EXIT_OK
    if not sys.stdin.isatty():
        _err(
            "approve asks you at a terminal, and takes --yes there alone: an approval is "
            "your word, never a script's"
        )
        return EXIT_USAGE
    for line in fact_lines(wf):
        print(line)
    for problem in here_problems(wf):
        print(f"  cannot run here yet: {_s(problem, 600)}")
    question = f"approve {wf.name}? A run takes it as it is now, by its sha256."
    if not _ask_yes(question, args.yes):
        print("not approved; nothing was signed")
        return EXIT_FAILED
    approval = approve(wf)
    state_said = "valid" if verify_file(approval.receipt).valid else "INVALID"
    print(f"approved {wf.name} {wf.short}: receipt {approval.run_id} ({state_said})")
    print(f"next: metabot workflow run {wf.name}")
    return EXIT_OK


def _versions(ref: str) -> list[str]:
    """The sha256 of every version a name has, or the one a sha256 names; KeyError for none."""
    named = [row.sha256 for row in stored_workflows() if row.name == ref]
    return named or [resolve(ref)]


def cmd_workflow_revoke(args: argparse.Namespace) -> int:
    from metabot.receipts import verify_file

    try:
        shas = _versions(args.ref)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    pending = [(sha, approval_state(sha).in_force) for sha in shas]
    pending = [(sha, found) for sha, found in pending if found]
    if not pending:
        print(f"nothing to revoke: no approval of {_s(args.ref, 80)} is in force")
        return EXIT_OK
    if not args.yes and not sys.stdin.isatty():
        _err("revoke asks first; run it in a terminal, or pass --yes")
        return EXIT_USAGE
    for sha, found in pending:
        print(f"  {sha[:12]}: {len(found)} approval{'' if len(found) == 1 else 's'} in force")
    question = f"revoke {_s(args.ref, 80)}? A run of it is refused until it is approved again."
    if not _ask_yes(question, args.yes):
        print("nothing was revoked")
        return EXIT_FAILED
    for sha, found in pending:
        path = revoke(sha, found)
        said = "valid" if verify_file(path).valid else "INVALID"
        print(f"revoked {sha[:12]}: receipt {path.name[: -len('.json')]} ({said})")
    return EXIT_OK


def cmd_workflow_schedule(args: argparse.Namespace) -> int:
    from metabot.routines import RoutineError, get_routine
    from metabot.workflow_firing import next_text, save_workflow_routine, summarize

    try:
        name = args.name or load_workflow(args.ref).name
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    except WorkflowError as exc:
        _err(f"routine not saved: {_s(exc, 600)}")
        return EXIT_FAILED
    try:
        existing = get_routine(name)["id"]
    except KeyError:
        existing = None
    try:
        rid = save_workflow_routine(name, args.ref, enabled=not args.off)
    except RoutineError as exc:
        _err(f"routine not saved: {_s(exc, 600)}")
        return EXIT_USAGE
    record = get_routine(rid)
    summary = summarize(record)
    done = "updated" if existing else "saved"
    state = "on" if record["enabled"] else "off"
    print(f"{done} routine {_s(name, 64)} ({rid}), {state}; next due {next_text(record)}")
    wf = summary.wf
    if wf is not None:
        print(
            f"  runs workflow {wf.name} {wf.short} on a schedule under a ceiling you approved: "
            f"{ceiling_text(wf)}"
        )
    if summary.approved_by is not None:
        print(
            f"  approved by {summary.approved_by.run_id}; each firing checks the approval and "
            "the workflow's sha256 again before a step runs"
        )
    print("  a version kept later under its name does not change it: schedule that one to fire it")
    print(f"next: metabot serve, or metabot routine run {_s(name, 64)} to fire it now")
    return EXIT_OK


def _receipt_said(path: Path | None) -> str:
    from metabot.receipts import verify_file

    if path is None:
        return "none"
    return "valid" if verify_file(path).valid else "INVALID"


def cmd_workflow_run(args: argparse.Namespace) -> int:
    import asyncio

    from metabot.console import Trace
    from metabot.paths import new_run_id
    from metabot.seals import seal_state
    from metabot.workflow_run import run_workflow

    try:
        wf = load_workflow(args.ref)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    except WorkflowError as exc:
        _err(f"workflow not run: {_s(exc, 600)}")
        return EXIT_FAILED
    try:
        approval = approval_for_run(wf)
    except NotApproved as exc:
        _err(f"workflow not run: {_s(exc, 600)}")
        return EXIT_FAILED
    problems = here_problems(wf)
    for problem in problems:
        _err(f"workflow not run: cannot run here: {_s(problem, 600)}")
    if problems:
        return EXIT_FAILED
    trace = Trace(quiet=args.quiet)
    run_id = new_run_id()
    print(f"workflow run {run_id}  {wf.name} {wf.short}, approved by {approval.run_id}", flush=True)
    try:
        run = asyncio.run(run_workflow(wf, approval, trace=trace, run_id=run_id))
    except (asyncio.CancelledError, KeyboardInterrupt):
        trace.line(f"[metabot] stopped; what ran is sealed: metabot receipts verify {run_id}")
        return 130
    for step in run.steps:
        line = (
            f"  {_s(step.name, 40):<12} {step.kind:<7} {'ok' if step.ok else 'error':<6} "
            f"{step.run_id or '-'}  receipt {_receipt_said(step.receipt)}"
        )
        if step.error:
            line += f"  {_s(step.error, 300)}"
        print(line)
    for left in wf.steps[len(run.steps) :]:
        print(f"  {_s(left.name, 40):<12} {left.kind:<7} not run")
    tty = sys.stdout.isatty()
    for step in run.steps:
        if step.answer:
            print(f"answer of {_s(step.name, 40)}:")
            print(terminal_safe(step.answer) if tty else step.answer, flush=True)
    sealed = run.seal is not None and seal_state(run.seal, run_id).startswith("valid")
    if run.seal is not None:
        print(f"sealed over every step: metabot receipts verify {run_id}")
    if run.seal_error:
        print(f"  {_s(run.seal_error, 300)}")
    return EXIT_OK if run.ok and sealed else EXIT_FAILED
