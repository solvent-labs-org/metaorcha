"""``metabot ui``: serve the local web UI on 127.0.0.1, behind a token made at start.

``metabot app`` (``metabot.web_window``) serves it the same way, and opens it in a window.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import signal
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from metabot.web_guard import Guard

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
LOOPBACK = "127.0.0.1"  # the UI listens on this address and no other


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _server(app: Any, uvicorn: Any, close: Callable[[], Awaitable[None]]) -> Any:
    """The server for ``app``; ``close`` is the UI's broker's close, run before uvicorn waits
    on the open connections, so a page's open event stream ends and the UI stops on one
    signal. uvicorn waits on connections with no time limit, and a run's page keeps a
    stream open for as long as the run goes: closing the broker first is what ends it.
    A second signal skips the UI's own shutdown, so the broker's close runs here too."""

    class Server(uvicorn.Server):
        """A stop signal stops the UI, which then exits 0, as ``serve`` does."""

        should_exit: bool  # uvicorn's own, set in its __init__
        force_exit: bool

        def handle_exit(self, sig, frame) -> None:  # uvicorn's own raises it again after
            if self.should_exit and sig == signal.SIGINT:
                self.force_exit = True
            else:
                self.should_exit = True

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            await close()  # every stream ends and every run is sealed; the UI closes it again
            await super().shutdown(sockets)

    return Server(uvicorn.Config(app, access_log=False, log_level="warning"))


def port_problem(port: int) -> str | None:
    """Why ``--port`` cannot be listened on, before anything is tried; None when it can."""
    return None if 0 <= port <= 65535 else f"metabot: --port {port} is not a port"


def address(guard: Guard) -> str:
    """The address that signs a browser in: it carries this start's token, once."""
    return f"http://{LOOPBACK}:{guard.port}/?token={guard.token}"


def serve_ui(port: int, ready: Callable[[Guard], bool]) -> int:
    """Serve the UI on ``port`` until it is stopped. ``ready`` is told the guard once the
    port listens, and hands the address on; when it says False, nothing is served."""
    import uvicorn

    from metabot.web_app import make_ui
    from metabot.web_guard import Guard

    problem = port_problem(port)
    if problem:
        _err(problem)
        return EXIT_USAGE
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOOPBACK, port))
        sock.listen(64)
    except OSError as exc:
        sock.close()
        _err(f"metabot: cannot listen on {LOOPBACK}:{port}: {exc.strerror or exc}")
        return EXIT_USAGE
    guard = Guard(secrets.token_urlsafe(32), sock.getsockname()[1])
    app, broker = make_ui(guard, start_dir=Path.cwd())
    if not ready(guard):
        sock.close()
        return EXIT_FAILED
    server = _server(app, uvicorn, broker.close)
    try:
        asyncio.run(server.serve(sockets=[sock]))
    except KeyboardInterrupt:
        # Ctrl-C is how the page is stopped, so it ends without a traceback
        pass
    finally:
        sock.close()
    _err("[metabot] ui stopped")
    return EXIT_OK


def cmd_ui(args: argparse.Namespace) -> int:
    def ready(guard: Guard) -> bool:
        print(f"metabot ui: {address(guard)}", flush=True)
        _err("[metabot] the address works until this stops; stop it with Ctrl-C")
        return True

    return serve_ui(args.port, ready)
