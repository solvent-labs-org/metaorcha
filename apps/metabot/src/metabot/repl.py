"""The chat loop: ``metabot`` (or ``metabot chat``) on a terminal.

Each line is one run with its own receipt. The engine's own session carries
the conversation from one line to the next, so a receipt always covers
exactly one turn.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from metabot import __version__
from metabot.approvals import terminal_safe
from metabot.console import Trace
from metabot.engines import ENGINES

HELP = """\
/engine NAME   switch engine ({engines}); starts a new conversation
/new           start a new conversation
/receipt       the last run's receipt and whether it verifies
/help          this list
/exit          leave (Ctrl-D works too)
Ctrl-C stops the run in progress; its receipt is still sealed."""


def _ask(**kwargs: Any) -> Any:
    from metabot.commands import ask_once

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
) -> int:
    """Read lines until /exit or end of input; each line is one run."""
    from metabot.commands import cancelled_result, receipt_line
    from metabot.receipts import answer_caveat, named_here, unread_caveat

    trace = trace or Trace()
    options = dict(options or {})
    session: str | None = None
    last: Any = None
    write(f"metabot {__version__}: {engine} in {cwd}. /help lists commands.")
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
                write(HELP.format(engines=", ".join(ENGINES)))
            elif command == "/new":
                session = None
                write("new conversation")
            elif command == "/engine":
                if arg in ENGINES:
                    engine, session = arg, None
                    options.pop("role", None)  # the role named the last engine
                    if options and options.pop("model", None):
                        write("the model you chose was for the last engine; using this one's own")
                    write(f"engine {engine}; new conversation")
                else:
                    write(f"unknown engine {arg!r}; choose one of: {', '.join(ENGINES)}")
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
    from metabot.commands import EXIT_USAGE, default_engine
    from metabot.roster import RosterError, pick

    try:
        engine, model = pick(args.role, args.engine, args.model)
    except RosterError as exc:
        print(f"metabot: {exc}")
        return EXIT_USAGE
    engine = engine or default_engine()
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"metabot: {cwd} is not a directory")
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
    return repl(input, print, engine=engine, cwd=cwd, options=options)
