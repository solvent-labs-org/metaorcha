"""The ``metabot`` subcommands: init, engines, ask, connect, receipts."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from metabot.approvals import Approver, pick_approver, printable, terminal_safe
from metabot.cmdline import (
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    read_prompt,
    resolve_allows,
)
from metabot.config import load_config, project_receipts_on, save_config
from metabot.connections import Connection, ConnectionStore
from metabot.console import Trace
from metabot.engines import BINARIES, ENGINES, available, get_engine
from metabot.gate import Policy
from metabot.paths import metabot_home, receipt_path, run_path
from metabot.prices import PRICES_AS_OF
from metabot.receipts import (
    answer_caveat,
    ensure_key,
    journal_matches,
    list_index,
    named_here,
    unread_caveat,
    verify_file,
)
from metabot.roster import RosterError, fast_model, fill, local_status, pick
from metabot.spend import dollars
from metabot.suggest import Suggestion, reason, suggest_here
from metabot.text import ascii_digits

ENGINE_ORDER = ("claude", "codex", "cursor", "openai")
SERVER_SAID_LIMIT = 4000
SERVER_SAID_LINES = 12


def _err(text: str) -> None:
    print(f"metabot: {text}", file=sys.stderr)


def default_engine() -> str:
    """The configured engine, else the first installed CLI, else the local endpoint."""
    cfg = load_config()
    if cfg.default_engine in ENGINES:
        return cfg.default_engine
    found = available()
    return next((n for n in ENGINE_ORDER if found.get(n)), "openai")


def cancelled_result(exc: BaseException) -> Any | None:
    """The sealed result of a cancelled run, also when Ctrl-C re-raised it."""
    from metabot.runner import RunCancelled

    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, RunCancelled):
            return seen.result
        seen = seen.__context__
    return None


# ── init / engines ──────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    _key, did = ensure_key()
    cfg = load_config()
    if args.engine:
        if args.engine not in ENGINES:
            _err(f"unknown engine {args.engine!r}; choose one of: {', '.join(ENGINES)}")
            return EXIT_USAGE
        cfg.default_engine = args.engine
    elif cfg.default_engine not in ENGINES:
        cfg.default_engine = default_engine()
    save_config(cfg)
    print(f"home      {metabot_home()}")
    print(f"signer    {did}")
    print(f"engine    {cfg.default_engine}")
    print('next      metabot ask "what is in this directory?"')
    return EXIT_OK


def _on_off(value: str) -> bool:
    if value.lower() in ("true", "on", "yes", "1"):
        return True
    if value.lower() in ("false", "off", "no", "0"):
        return False
    raise ValueError("true or false")


def _fan_cap(value: str) -> int:
    from metabot.office import FAN_CAP_MAX

    if not ascii_digits(value) or not 1 <= int(value) <= FAN_CAP_MAX:
        raise ValueError(f"a whole number from 1 to {FAN_CAP_MAX}")
    return int(value)


def _attempts(value: str) -> int:
    from metabot.ladder import ATTEMPTS_MAX

    if not ascii_digits(value) or not 1 <= int(value) <= ATTEMPTS_MAX:
        raise ValueError(f"a whole number from 1 to {ATTEMPTS_MAX}")
    return int(value)


SETTINGS = {
    "project_receipts": (_on_off, "also copy each receipt to <project>/.orcha/receipts/"),
    "fan_cap": (_fan_cap, "how many desk runs go at once"),
    "ladder_attempts": (_attempts, "how many rungs of the ladder one task may run"),
}


def cmd_config_show(_args: argparse.Namespace) -> int:
    cfg = load_config()
    for name, (_parse, about) in SETTINGS.items():
        print(f"{name:<18} {json.dumps(getattr(cfg, name)):<8} {about}")
    return EXIT_OK


def cmd_config_set(args: argparse.Namespace) -> int:
    if args.key not in SETTINGS:
        _err(f"no setting {args.key!r}; one of: {', '.join(SETTINGS)}")
        return EXIT_USAGE
    parse, _about = SETTINGS[args.key]
    try:
        value = parse(args.value)
    except ValueError as exc:
        _err(f"{args.key} takes {exc}")
        return EXIT_USAGE
    cfg = load_config()
    setattr(cfg, args.key, value)
    save_config(cfg)
    print(f"{args.key} {json.dumps(value)}")
    return EXIT_OK


def cmd_engines(_args: argparse.Namespace) -> int:
    cfg = load_config()
    found = available()
    chosen = default_engine()
    for name in ENGINES:
        mark = "*" if name == chosen else " "
        if name == "openai":
            status = f"{cfg.openai_base_url} model {cfg.openai_model}: "
            status += local_status(cfg.openai_base_url)
        else:
            status = found.get(name) or f"not installed ({BINARIES[name]} not on PATH)"
        print(f"{mark} {name:<7} {status}")
    return EXIT_OK


# ── ask ─────────────────────────────────────────────────────────────────────


async def ask_once(
    *,
    engine_name: str,
    prompt: str,
    cwd: Path,
    model: str | None = None,
    allow_write: bool = False,
    allows: list[str] | None = None,
    read_only: bool = False,
    resume: str | None = None,
    connections: bool = True,
    plugins: bool = True,
    trace: Trace | None = None,
    role: str | None = None,
    suggested: dict[str, Any] | None = None,
    project_receipt: bool | None = None,
    approver: Approver | None = None,
    run_id: str | None = None,
    engine: Any = None,
    limits: Any = None,
    extra_meta: Mapping[str, Any] | None = None,
    receipt_project: Path | None = None,
) -> Any:
    """One run as ``ask`` makes it. ``approver`` answers the gate, by default this terminal;
    ``engine`` is the engine itself, for a caller that makes its own; ``extra_meta`` joins
    the run's meta; ``receipt_project`` is the project a receipt copy goes to, when the run
    works somewhere else, as a ladder rung in its copy does."""
    from metabot.plugins import enabled_plugins
    from metabot.runner import run_job

    engine = engine if engine is not None else get_engine(engine_name)
    chosen = enabled_plugins() if plugins else []
    store = ConnectionStore()
    allowed = resolve_allows(allows or [], engine, store)
    policy = Policy(allow_write=allow_write, allows=allowed, interactive=True)
    meta: dict[str, Any] = {"plugins": [p.pin for p in chosen]} if chosen else {}
    if role:
        meta["role"] = role
    if suggested is not None:
        meta["suggested"] = dict(suggested)
    meta.update(extra_meta or {})
    return await run_job(
        engine=engine,
        prompt=prompt,
        cwd=cwd,
        policy=policy,
        approver=approver if approver is not None else pick_approver(),
        model=model or load_config().models.get(engine_name),
        read_only=read_only,
        resume=resume,
        run_id=run_id,
        plugin_dirs=[p.view for p in chosen],
        switchboard=connections and bool(store.active()),
        on_event=trace,
        meta=meta or None,
        project=(receipt_project or cwd) if project_receipts_on(project_receipt) else None,
        limits=limits,
    )


def cost_line(result: Any, engine_name: str) -> str:
    """What a run cost, and where the figure comes from, for the line beside its receipt."""
    cost, source = result.cost_musd, result.cost_source
    if source == "engine" and cost is not None:
        return f"[metabot] cost {dollars(cost)}, as {engine_name} reported it"
    if source == "local":
        return "[metabot] cost $0.00: the model runs on this machine"
    if source in ("table", "config") and cost is not None:
        whose = "the listed" if source == "table" else "your"
        return f"[metabot] cost {dollars(cost)}, by {whose} rates"
    if source == "plan":
        return f"[metabot] {result.tokens} tokens on your {engine_name} plan; MetaBot has no price"
    return f"[metabot] {result.tokens} tokens; no price for this model: add one under prices"


def receipt_line(result: Any) -> tuple[str, bool]:
    valid = verify_file(result.receipt).valid
    state = "valid" if valid else "INVALID"
    return f"[metabot] receipt {result.receipt} ({state}, {result.steps} steps)", valid


def cmd_ask(args: argparse.Namespace) -> int:
    from metabot.runner import Limits, NoPrice, RunCancelled

    trace = Trace(quiet=args.quiet)
    if args.check and not args.ladder:
        _err("--check goes with --ladder: a failing check makes the ladder climb")
        return EXIT_USAGE
    if args.ladder:
        return _ask_ladder(args, trace)
    prompt: str | None = None
    suggested: Suggestion | None = None
    engine_name: str | None = None
    role = args.role
    if args.suggest:
        if role:
            _err("--suggest picks the role itself; pass --suggest or --role, not both")
            return EXIT_USAGE
        prompt = read_prompt(args.prompt)
        suggested = suggest_here(prompt)
        if suggested.engine is None:
            _err(suggested.note or "nothing here can run this task")
            return EXIT_USAGE
        role = suggested.role
        engine_name, model = fill(suggested.entry(), args.engine, args.model)
    else:
        try:
            engine_name, model = pick(role, args.engine, args.model)
        except RosterError as exc:
            _err(str(exc))
            return EXIT_USAGE
    engine_name = engine_name or default_engine()
    if engine_name not in ENGINES:
        _err(f"unknown engine {engine_name!r}; choose one of: {', '.join(ENGINES)}")
        return EXIT_USAGE
    if role is None and args.engine is None and model is None:  # a plain ask
        model = fast_model(engine_name)
        if model:
            trace.note(f"[metabot] model {model}, from the fast role; -m picks another")
    if prompt is None:
        prompt = read_prompt(args.prompt)
    if not prompt.strip():
        _err("the prompt is empty")
        return EXIT_USAGE
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        _err(f"{cwd} is not a directory")
        return EXIT_USAGE
    if suggested is not None:
        why = reason(suggested)
        said = f"[metabot] suggested the {suggested.role} role (rule {why}): {suggested.engine}"
        if (engine_name, model) != (suggested.engine, suggested.model):
            said += f"; you chose {engine_name}{'/' + model if model else ''}"
        trace.note(said)
        if suggested.note:
            trace.note(f"[metabot] {suggested.note}")
    try:
        result = asyncio.run(
            ask_once(
                engine_name=engine_name,
                prompt=prompt,
                cwd=cwd,
                model=model,
                allow_write=args.allow_write,
                allows=args.allow,
                read_only=args.read_only,
                resume=args.resume,
                connections=not args.no_connections,
                plugins=not args.no_plugins,
                trace=trace,
                role=role,
                suggested=suggested.record() if suggested is not None else None,
                project_receipt=args.project_receipt,
                limits=Limits(max_cost_musd=args.max_cost) if args.max_cost else None,
            )
        )
    except NoPrice as exc:
        _err(str(exc))
        return EXIT_USAGE
    except (RunCancelled, KeyboardInterrupt) as exc:
        cancelled = cancelled_result(exc)
        tail = f"; receipt {cancelled.receipt}" if cancelled is not None else ""
        trace.line(f"[metabot] cancelled{tail}")
        return EXIT_CANCELLED
    valid = _show_result(args, trace, result, engine_name, prompt, cwd)
    return EXIT_OK if result.ok and valid else EXIT_FAILED


def _show_result(
    args: argparse.Namespace,
    trace: Trace,
    result: Any,
    engine_name: str,
    prompt: str,
    cwd: Path,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Print a run's answer and its lines as ``ask`` does; whether its receipt is valid."""
    line, valid = receipt_line(result)
    copy = result.project_receipt
    if args.json:
        print(
            json.dumps(
                {
                    **asdict(result),
                    "receipt": str(result.receipt),
                    "project_receipt": str(copy) if copy else None,
                    "valid": valid,
                    **(extra or {}),
                }
            )
        )
    else:
        if result.final_text:  # the answer first, also when piped; exact in a pipe
            text = result.final_text
            print(terminal_safe(text) if sys.stdout.isatty() else text, flush=True)
            caveat = answer_caveat(result.declined, result.failed)
            if caveat:  # about the answer itself, so said even when quiet
                trace.line(f"[metabot] {caveat}")
            unread = None if result.steps or args.resume else unread_caveat(named_here(prompt, cwd))
            if unread:  # an earlier turn of a resumed run may have read them
                trace.line(f"[metabot] {unread}")
        trace.note(line)
        trace.note(cost_line(result, engine_name))
        if copy is not None:
            trace.note(f"[metabot] project copy {copy}")
    if result.project_error:  # asked for, so said even when quiet
        trace.line(f"[metabot] {result.project_error}; the receipt in the MetaBot home counts")
    return valid


def _ask_ladder(args: argparse.Namespace, trace: Trace) -> int:
    """``ask --ladder``: the cheapest rung first, climbing only on a failure the record shows."""
    from metabot.checks import refusal
    from metabot.engines import get_engine
    from metabot.ladder import LadderError, Rung, attempts, effective_ladder, run_ladder, spent_line
    from metabot.ladder_copies import Copies, CopyError
    from metabot.paths import new_run_id
    from metabot.roster import RosterError
    from metabot.runner import Limits, NoPrice, RunCancelled
    from metabot.suggest import match

    clash = [
        flag
        for flag, given in (
            ("-e", args.engine),
            ("-m", args.model),
            ("--suggest", args.suggest),
            ("--resume", args.resume),
        )
        if given
    ]
    if clash or args.role not in (None, "private"):
        said = clash[0] if clash else f"--role {args.role}"
        _err(f"--ladder picks each rung itself; {said} does not go with it (--role private does)")
        return EXIT_USAGE
    prompt = read_prompt(args.prompt)
    if not prompt.strip():
        _err("the prompt is empty")
        return EXIT_USAGE
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        _err(f"{cwd} is not a directory")
        return EXIT_USAGE
    checks = {f"ask:{n}": command for n, command in enumerate(args.check or [], 1)}
    refused = refusal(checks)
    if refused:
        _err(refused.replace("change them in office.yaml", "change the --check"))
        return EXIT_USAGE
    cfg = load_config()
    try:
        rungs, whose, note = effective_ladder(cfg)
        most = attempts(cfg)
    except (LadderError, RosterError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    if note:
        trace.note(f"[metabot] {note}")
    if not rungs:
        _err("no rung here: install an engine CLI, or start and probe the local model server")
        return EXIT_USAGE
    rule, _word = match(prompt)
    private = args.role == "private" or (rule is not None and rule.role == "private")
    trace.note(
        f"[metabot] ladder ({whose}): "
        + " -> ".join(f"{r.tier} {r.label()}" for r in rungs[:most])
        + ("; private, so only rungs on this machine" if private else "")
    )

    ladder_id = new_run_id()
    copies = None
    if not args.read_only:  # a read-only rung changes nothing, so it needs no copy
        try:
            copies = Copies.start(cwd, ladder_id)
        except CopyError as exc:
            trace.line(f"[metabot] {exc}; every rung works here")
        if copies is None:
            trace.note("[metabot] not a git checkout: every rung works here, in place")
        else:
            trace.note(
                f"[metabot] each rung works in its own copy of the project ({copies.base[:12]})"
            )

    async def run_rung(
        rung: Rung, run_id: str, meta: dict[str, Any], cap: int | None, where: Path | None
    ) -> Any:
        trace.note(f"[metabot] rung {meta['rung']}: {rung.label()} ({rung.tier})")
        return await ask_once(
            engine_name=rung.engine,
            prompt=prompt,
            cwd=where or cwd,
            receipt_project=cwd,
            model=rung.model,
            allow_write=args.allow_write,
            allows=args.allow,
            read_only=args.read_only,
            connections=not args.no_connections,
            plugins=not args.no_plugins,
            trace=trace,
            role=args.role,
            project_receipt=args.project_receipt,
            run_id=run_id,
            limits=Limits(max_cost_musd=cap) if cap is not None else None,
            extra_meta=meta,
        )

    def gated(engine: str) -> bool:
        return bool(getattr(get_engine(engine), "own_tools_gated", False))

    try:
        ladder = asyncio.run(
            run_ladder(
                rungs,
                run_rung,
                cfg=cfg,
                ladder_id=ladder_id,
                max_attempts=most,
                max_cost_musd=args.max_cost,
                private=private,
                gated=gated,
                on_line=lambda text: trace.note(f"[metabot] {text}"),
                no_price=NoPrice,
                checks=checks or None,
                copies=copies,
                task=prompt,
                cwd=cwd,
            )
        )
    except (RunCancelled, KeyboardInterrupt) as exc:
        cancelled = cancelled_result(exc)
        tail = f"; receipt {cancelled.receipt}" if cancelled is not None else ""
        trace.line(f"[metabot] cancelled{tail}")
        return EXIT_CANCELLED
    result = ladder.final
    if ladder.stopped:  # why there is no answer, so said even when quiet
        trace.line(f"[metabot] the ladder stopped: {ladder.stopped}")
    if ladder.applied is False and ladder.apply_note:  # the work is not in the project
        trace.line(f"[metabot] {ladder.apply_note}")
    if result is None:
        if args.json:
            print(json.dumps({"ok": False, "ladder": ladder.to_json()}))
        return EXIT_FAILED
    engine_name = next(r.engine for r in ladder.rows if r.run_id == result.run_id)
    valid = _show_result(
        args, trace, result, engine_name, prompt, cwd, {"ladder": ladder.to_json()}
    )
    trace.note(f"[metabot] {spent_line(ladder)}")
    return EXIT_OK if ladder.answered and valid else EXIT_FAILED


# ── connect ─────────────────────────────────────────────────────────────────


def _read_token(name: str, no_token: bool) -> str | None:
    if no_token:
        return None
    if sys.stdin.isatty():
        token = getpass.getpass(f"token for {name} (hidden; empty for none): ")
    else:
        token = sys.stdin.read()
    return token.strip() or None


def cmd_connect_add(args: argparse.Namespace) -> int:
    from metabot.origins import origins_for

    if bool(args.command) == bool(args.url):
        _err("give exactly one of --url URL or --command CMD ...")
        return EXIT_USAGE
    if args.origin and not args.browser:
        _err("--origin is for a browser connection; add --browser")
        return EXIT_USAGE
    try:
        origins = origins_for(args.origin or []) if args.browser else None
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    token = _read_token(args.name, args.no_token)
    try:
        conn = ConnectionStore().add(
            name=args.name,
            transport="stdio" if args.command else "http",
            command=args.command or None,
            url=args.url,
            token=token,
            token_env=args.token_env,
            header=args.header,
            origins=origins,
        )
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    print(f"added {conn.name} ({conn.id}){'' if token else ' without a token'}")
    if conn.origins is not None:
        print(
            f"a browser: on {', '.join(conn.origins)} a call is read or write by its name; "
            "anywhere else every call is destructive"
        )
    print(f"check it with: metabot connect test {conn.name}")
    return EXIT_OK


def cmd_connect_list(_args: argparse.Namespace) -> int:
    conns = ConnectionStore().list()
    if not conns:
        print("no connections; add one with: metabot connect add NAME --command ...")
    for conn in conns:
        target = " ".join(conn.command or []) if conn.transport == "stdio" else conn.url
        shown = printable(str(target), 300)  # a plugin can add a connection: not always typed here
        print(f"{conn.name:<16} {conn.state:<8} {conn.transport:<5} {conn.id}  {shown}")
        if conn.origins is not None:
            print(f"  origins {printable(', '.join(conn.origins), 600) or '(none)'}")
    return EXIT_OK


def _active(store: ConnectionStore, name: str) -> Connection:
    conn = store.get(name)  # KeyError: no such connection
    if not conn.active:
        raise KeyError(f"{conn.name} is {conn.state}")
    return conn


async def _list_tools(conn: Connection, secret: str | None, errlog: Any) -> list[Any]:
    from metabot.upstream import Upstream

    async with Upstream(conn, secret, errlog=errlog) as upstream:
        return await upstream.list_tools()


def _tools_or_error(store: ConnectionStore, name: str) -> tuple[Connection, list[Any]] | int:
    """The connection's tools; a server's own stderr is shown only when it fails."""
    try:
        conn = _active(store, name)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    secret = store.secret(conn)
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
        try:
            return conn, asyncio.run(_list_tools(conn, secret, errlog))
        except Exception as exc:  # a connection that fails is named, not a traceback
            _err(f"{name} did not answer: {type(exc).__name__}: {printable(str(exc), 300)}")
            errlog.seek(0)
            said = errlog.read()
            if secret:  # redact before cutting, so no part of the token survives the cut
                said = said.replace(secret, f"[REDACTED:{conn.redacted_as}]")
            for line in said[-SERVER_SAID_LIMIT:].splitlines()[-SERVER_SAID_LINES:]:
                print(f"  {name}: {printable(line, 200)}", file=sys.stderr)
            return EXIT_FAILED


def cmd_connect_test(args: argparse.Namespace) -> int:
    got = _tools_or_error(ConnectionStore(), args.name)
    if isinstance(got, int):
        return got
    conn, tools = got
    names = ", ".join(printable(t.name, 80) for t in tools)
    print(f"{conn.name}: {len(tools)} tools: {names}")
    return EXIT_OK


def cmd_connect_scope(args: argparse.Namespace) -> int:
    from metabot.switchboard import class_label

    store = ConnectionStore()
    try:
        for item in args.set or []:
            capability, sep, cls = item.partition("=")
            if not sep or not capability:
                raise ValueError("--set takes TOOL=CLASS")
            store.set_override(args.name, capability, cls)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    got = _tools_or_error(store, args.name)
    if isinstance(got, int):
        return got
    conn, tools = got
    for tool in tools:
        mine = conn.overrides.get(tool.name)
        note = f"  (you set {mine})" if mine else ""
        print(f"{printable(tool.name, 80):<32} {class_label(conn, tool)}{note}")
    listed = {tool.name for tool in tools}
    for name, mine in sorted(conn.overrides.items()):
        if name not in listed:  # a misspelt name tightens nothing, so it is shown
            said = f"(you set it; {printable(conn.name, 32)} lists no such tool)"
            print(f"{printable(name, 80):<32} {mine}  {said}")
    if conn.overrides:
        print("a setting can only tighten a class; a looser one has no effect")
    return EXIT_OK


def cmd_connect_revoke(args: argparse.Namespace) -> int:
    try:
        conn = ConnectionStore().revoke(args.name)
    except KeyError as exc:
        _err(str(exc.args[0]))
        return EXIT_USAGE
    print(f"revoked {conn.name} ({conn.id}); any token it had is deleted")
    return EXIT_OK


# ── receipts ────────────────────────────────────────────────────────────────


def _receipt_path(ref: str) -> Path | None:
    path = Path(ref)
    if path.is_file():
        return path
    try:
        path = receipt_path(ref)
    except ValueError:
        return None
    return path if path.is_file() else None


def cmd_receipts_list(args: argparse.Namespace) -> int:
    rows = list_index()[-args.limit :] if args.limit > 0 else []
    if not rows:
        print("no runs yet")
    for row in rows:
        engine = str(row.get("engine", "?"))
        status = str(row.get("status", "?"))
        when = row.get("finished_at", "?")
        print(f"{row['run_id']}  {when}  {engine:<7} {status:<9} {row.get('steps', 0)} steps")
    return EXIT_OK


# Where a run's cost came from, as ``metabot cost`` says it.
COST_SOURCES = {
    "engine": "as the engine reported it",
    "table": "by the listed rates",
    "config": "by your rates",
    "local": "on this machine",
    "plan": "on your plan, not priced",
    "unknown": "no price",
}


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cmd_cost(args: argparse.Namespace) -> int:
    """What past runs spent, from the run index, by engine and model."""
    rows = list_index()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    for row in rows:
        key = (_shown(row.get("engine", "?"), 40), _shown(row.get("model") or "default", 80))
        group = groups.setdefault(
            key, {"runs": 0, "tokens": 0, "cost_musd": None, "sources": set()}
        )
        group["runs"] += 1
        group["tokens"] += _whole(row.get("tokens")) or 0
        cost = _whole(row.get("cost_musd"))
        if cost is not None:
            group["cost_musd"] = (group["cost_musd"] or 0) + cost
            total += cost
        source = row.get("cost_source")
        group["sources"].add(source if source in COST_SOURCES else "not recorded")
    if args.json:
        by_model = [
            {"engine": e, "model": m, **g, "sources": sorted(g["sources"])}
            for (e, m), g in groups.items()
        ]
        print(json.dumps({"runs": len(rows), "total_musd": total, "prices_as_of": PRICES_AS_OF,
                          "by_model": by_model}))  # fmt: skip
        return EXIT_OK
    if not rows:
        print("no runs yet")
        return EXIT_OK
    for (engine, model), g in groups.items():
        spent = "-" if g["cost_musd"] is None else dollars(g["cost_musd"])
        said = "; ".join(COST_SOURCES.get(s, s) for s in sorted(g["sources"]))
        print(f"{engine}/{model}  {g['runs']} runs  {g['tokens']} tokens  {spent}  {said}")
    print(f"total {dollars(total)} over {len(rows)} runs; prices as of {PRICES_AS_OF}")
    print("from the run index; metabot receipts verify RUN checks a run's signed figure")
    return EXIT_OK


def _shown(value: object, limit: int) -> str:
    """A journal field for the terminal: the journal is a file anyone with the home can edit."""
    return printable(str(value), limit)


def cmd_receipts_show(args: argparse.Namespace) -> int:
    from metabot.office_commands import show_office
    from metabot.office_run import load_record

    record = load_record(args.run)
    if record is not None:  # an office run: the tree of its desks
        return show_office(record)
    try:
        journal = json.loads((run_path(args.run) / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _err(f"no run {args.run!r} ({type(exc).__name__})")
        return EXIT_USAGE
    meta = journal.get("meta", {})
    print(f"run       {_shown(journal.get('run_id'), 130)}")
    engine, model = _shown(meta.get("engine"), 40), _shown(meta.get("model") or "", 120)
    print(f"engine    {engine} {model}".rstrip())
    print(f"status    {_shown(meta.get('status'), 40)}")
    print(f"prompt    {printable(str(meta.get('prompt', '')), 300)}")
    for step in journal.get("steps", []):
        mark = "ok  " if step.get("success") else "FAIL"
        print(f"  {mark} {printable(str(step.get('tool', '')), 80)}")
    for verdict in journal.get("verdicts", []):
        detail = printable(str(verdict.get("detail", "")), 200)
        check, result = _shown(verdict.get("check"), 160), _shown(verdict.get("result"), 20)
        print(f"  verdict {check}: {result} {detail}".rstrip())
    if meta.get("final"):
        print(f"answer    {printable(str(meta['final']), 600)}")
    print(f"receipt   {_shown(journal.get('receipt'), 600)}")
    return EXIT_OK


def cmd_receipts_verify(args: argparse.Namespace) -> int:
    path = _receipt_path(args.ref)
    if path is None:
        _err(f"no receipt {args.ref!r} (give a run id or a receipt file)")
        return EXIT_USAGE
    verdict = verify_file(path)
    checks = "  " + ", ".join(f"{k} {'ok' if v else 'FAILED'}" for k, v in verdict.checks.items())
    if not verdict.valid:
        print(f"INVALID  {path}")
        print(checks)
        return EXIT_FAILED
    run_id = verdict.run_id or ""
    try:
        kept_here = receipt_path(run_id).resolve() == path.resolve()
    except ValueError:
        kept_here = False
    same = journal_matches(run_id) if kept_here else None
    # The first line is the verdict the exit code gives: a journal that does not
    # match makes the whole check fail, though the receipt's own checks pass.
    print(f"{'INVALID' if same is False else 'VALID'}  {path}")
    print(checks)
    print(f"  signer {verdict.signer_did}, {verdict.step_count} steps")
    if same is not None:
        print(f"  journal {'re-derives every hash' if same else 'DOES NOT MATCH the receipt'}")
    return EXIT_FAILED if same is False else EXIT_OK
