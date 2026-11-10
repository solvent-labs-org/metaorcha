"""The chat loop: ``metabot`` (or ``metabot chat``) on a terminal.

Each line is one run with its own receipt. The engine's own session carries
the conversation from one line to the next, so a receipt always covers
exactly one turn.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from metabot import __version__
from metabot.approvals import printable, terminal_safe
from metabot.console import Trace
from metabot.engines import engine_names, known_engine

HELP = """\
/engine NAME   switch engine ({engines}); starts a new conversation
/new           start a new conversation
/receipt       the last run's receipt and whether it verifies
/help          this list
/exit          leave (Ctrl-D works too)
Ctrl-C stops the run in progress; its receipt is still sealed."""


def _ask(**kwargs: Any) -> Any:
    from metabot.commands import ask_once

    login = kwargs.pop("login", None)
    if login:  # each turn starts the engine on the login's folder, as ask --login does
        from metabot.engines import get_engine

        kwargs["engine"] = get_engine(kwargs["engine_name"], login=login)
    return asyncio.run(ask_once(**kwargs))


def repl(
    read_line: Callable[[str], str],
    write: Callable[[str], None],
    *,
    engine: str,
    cwd: Path,
    trace: Trace | None = None,
    run: Callable[..., Any] = _ask,
    options: dict[str, Any] | None = None,
    login: str | None = None,
) -> int:
    """Read lines until /exit or end of input; each line is one run."""
    from metabot.commands import cancelled_result, receipt_line
    from metabot.receipts import answer_caveat, named_here, unread_caveat

    trace = trace or Trace()
    options = dict(options or {})
    session: str | None = None
    last: Any = None
    on = f" on the login {login}" if login else ""
    write(
        f"metabot {__version__}: {engine}{on} in {printable(str(cwd), 2000)}. /help lists commands."
    )
    while True:
        try:
            line = read_line(f"{engine}> ")
        except EOFError:
            write("")
            return 0
        except KeyboardInterrupt:
            write("")
            continue
        text = line.strip()
        if not text:
            continue
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            arg = arg.strip()
            if command in ("/exit", "/quit"):
                return 0
            if command == "/help":
                write(HELP.format(engines=", ".join(engine_names())))
            elif command == "/new":
                session = None
                write("new conversation")
            elif command == "/engine":
                if login and arg != engine:
                    write(
                        f"this chat runs on the {engine} login {login};"
                        f" start a new chat for {arg or 'another engine'}"
                    )
                elif known_engine(arg):
                    engine, session = arg, None
                    options.pop("role", None)  # the role named the last engine
                    if options and options.pop("model", None):
                        write("the model you chose was for the last engine; using this one's own")
                    write(f"engine {engine}; new conversation")
                else:
                    write(f"unknown engine {arg!r}; choose one of: {', '.join(engine_names())}")
            elif command == "/receipt":
                write(receipt_line(last)[0] if last is not None else "no run yet")
            else:
                write(f"unknown command {command}; /help lists them")
            continue
        resumed = session is not None  # an earlier turn may have read what this one names
        try:
            result = run(
                engine_name=engine,
                prompt=text,
                cwd=cwd,
                resume=session,
                trace=trace,
                **({"login": login} if login else {}),
                **options,
            )
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            cancelled = cancelled_result(exc)
            if cancelled is not None:
                last = cancelled
                session = cancelled.session_id or session
                write(f"cancelled; receipt {cancelled.receipt}")
            else:
                write("cancelled")
            continue
        last = result
        session = result.session_id or session
        if result.final_text:
            write(terminal_safe(result.final_text))
            caveat = answer_caveat(result.declined, result.failed)
            if caveat:
                trace.line(f"[metabot] {caveat}")
            unread = None if result.steps or resumed else unread_caveat(named_here(text, cwd))
            if unread:
                trace.line(f"[metabot] {unread}")
        trace.line(receipt_line(result)[0])


def cmd_chat(args: argparse.Namespace) -> int:
    from metabot.cmdline import given_folder
    from metabot.commands import EXIT_USAGE, cleared_line, default_engine
    from metabot.config import load_config
    from metabot.engines import get_engine
    from metabot.logins import run_login
    from metabot.roster import RosterError, pick, role_login

    try:
        engine, model = pick(args.role, args.engine, args.model)
        login, why = run_login(
            getattr(args, "login", None),
            args.role,
            role_login(args.role, engine) if args.role and engine else None,
            None,
        )
    except (RosterError, ValueError) as exc:
        print(f"metabot: {exc}")
        return EXIT_USAGE
    if login is not None and engine is None:  # a login names its engine
        spec = load_config().logins.get(login)
        engine = spec.get("engine") if isinstance(spec, dict) else None
    engine = engine or default_engine()
    if login is not None:
        try:  # refuse before the first turn, not at it
            cleared = get_engine(engine, login=login).cleared
        except ValueError as exc:  # ConfigError is one: an unknown login
            print(f"metabot: {exc}")
            return EXIT_USAGE
        if why:
            print(why)
        if cleared:
            print(cleared_line(login, cleared))
    cwd, usable = given_folder(args.cwd)
    if not usable:
        print(f"metabot: {printable(str(cwd), 2000)} is not a directory")
        return EXIT_USAGE
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing and history for input())
    options = {
        "model": model,
        "role": args.role,
        "allow_write": args.allow_write,
        "allows": args.allow,
        "read_only": args.read_only,
        "connections": not args.no_connections,
        "plugins": not args.no_plugins,
    }
    return repl(input, print, engine=engine, cwd=cwd, options=options, login=login)
