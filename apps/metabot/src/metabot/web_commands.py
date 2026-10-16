"""``metabot ui``: serve the local web UI on 127.0.0.1, behind a token made at start."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import signal
import socket
import sys
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_USAGE = 0, 2
LOOPBACK = "127.0.0.1"  # the UI listens on this address and no other


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _server(app: Any, uvicorn: Any) -> Any:
    class Server(uvicorn.Server):
        """A stop signal stops the UI, which then exits 0, as ``serve`` does."""

        should_exit: bool  # uvicorn's own, set in its __init__
        force_exit: bool

        def handle_exit(self, sig, frame) -> None:  # uvicorn's own raises it again after
            if self.should_exit and sig == signal.SIGINT:
                self.force_exit = True
            else:
                self.should_exit = True

    return Server(uvicorn.Config(app, access_log=False, log_level="warning"))


def cmd_ui(args: argparse.Namespace) -> int:
    import uvicorn

    from metabot.web_app import make_app
    from metabot.web_guard import Guard

    if not 0 <= args.port <= 65535:
        _err(f"metabot: --port {args.port} is not a port")
        return EXIT_USAGE
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOOPBACK, args.port))
        sock.listen(64)
    except OSError as exc:
        sock.close()
        _err(f"metabot: cannot listen on {LOOPBACK}:{args.port}: {exc.strerror or exc}")
        return EXIT_USAGE
    port = sock.getsockname()[1]
    guard = Guard(secrets.token_urlsafe(32), port)
    app = make_app(guard, start_dir=Path.cwd())
    print(f"metabot ui: http://{LOOPBACK}:{port}/?token={guard.token}", flush=True)
    _err("[metabot] the address works until this stops; stop it with Ctrl-C")
    server = _server(app, uvicorn)
    try:
        asyncio.run(server.serve(sockets=[sock]))
    except KeyboardInterrupt:
        # Ctrl-C is how the page is stopped, so it ends without a traceback
        pass
    finally:
        sock.close()
    _err("[metabot] ui stopped")
    return EXIT_OK
