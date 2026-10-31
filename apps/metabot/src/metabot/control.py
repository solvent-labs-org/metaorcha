"""The runner's private channel: one Unix socket per run, owner-only.

Newline-delimited JSON, one request per connection. The switchboard and the
gate hook run as children of the engine, so they cannot write the run's
journal themselves; they send the runner approvals to decide and steps to
record, and the runner stays the only writer.

Ops: ``{"op": "approve", ...}`` -> ``{"allow": bool, "reason": str}``;
``{"op": "step", "step": {...}}`` -> ``{"ok": true}``; anything else ->
``{"error": "unknown op"}``.

An asker that hangs up before its reply, as one that gave up waiting does,
withdraws its request: a person still asked is asked no longer, and the call
is declined. A step sent before the hang-up is still taken.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_MESSAGE = 32 * 1024 * 1024
SUN_PATH_LIMIT = 100  # sockaddr_un holds 104-108 bytes, platform dependent
WITHDRAWN = {"allow": False, "reason": "declined: cancelled"}  # a question the run took back


def _fits(path: Path) -> bool:
    return len(os.fsencode(str(path))) <= SUN_PATH_LIMIT


def socket_path(run_dir: Path) -> Path:
    """The run's socket path; a private temp dir when the run dir's path is too long.

    The temp dir goes in the system's temp folder, or in /tmp when that
    folder's own path is too long for a socket. Whoever serves the socket
    removes the temp dir: ``ControlServer(..., cleanup_dir=True)``.
    """
    candidate = run_dir / "control.sock"
    if _fits(candidate):
        return candidate
    for base in dict.fromkeys((tempfile.gettempdir(), "/tmp")):
        if os.path.isdir(base) and _fits(Path(base) / "mb-xxxxxxxx" / "control.sock"):
            return Path(tempfile.mkdtemp(prefix="mb-", dir=base)) / "control.sock"  # 0700
    raise ValueError(f"no folder has a path short enough for the run's socket: {candidate}")


class ControlServer:
    def __init__(
        self,
        path: Path,
        *,
        on_approve: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        on_step: Callable[[dict[str, Any]], None],
        cleanup_dir: bool = False,
    ) -> None:
        self.path = path
        self.on_approve = on_approve
        self.on_step = on_step
        self.cleanup_dir = cleanup_dir
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[Any]] = set()
        self._asking: set[asyncio.Task[Any]] = set()  # questions still waiting on an answer
        self._closing = False

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server: asyncio.Server | None = None
        try:
            # Its folder is the run's own, which no one else can enter, until it is made
            # owner-only. The file mode mask is left as it is: every thread shares it.
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server = await asyncio.start_unix_server(
                self._handle, sock=sock, limit=MAX_MESSAGE, start_serving=False
            )
            await server.start_serving()  # held first, so a stop here closes it whole
        except BaseException:
            if server is not None:
                server.close()  # the loop stops watching the socket before it is closed
            else:
                sock.close()
            raise
        self._server = server

    async def close(self) -> None:
        """Stop serving. A question still waiting (on a person, say) is cancelled:
        closing never waits on an answer, and the asker reads no reply as a no."""
        self._closing = True
        if self._server is not None:
            self._server.close()
            pending = list(self._handlers)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        if self.cleanup_dir:
            with contextlib.suppress(OSError):
                self.path.parent.rmdir()

    def withdraw(self) -> None:
        """Take back every question still waiting on an answer, as the run ends: each is
        declined, ``declined: cancelled``, and the person is asked no longer. Steps are still
        taken, as the engine stops."""
        for task in list(self._asking):
            task.cancel()

    async def _dispatch(self, msg: Any) -> dict[str, Any]:
        if not isinstance(msg, dict):
            return {"error": "a request is a JSON object"}
        op = msg.get("op")
        if op == "approve":
            return await self.on_approve(msg)
        if op == "step":
            step = msg.get("step")
            if not isinstance(step, dict):
                return {"error": "step must be an object"}
            self.on_step(step)
            return {"ok": True}
        return {"error": "unknown op"}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            if self._closing:
                return
            line = await reader.readline()
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):  # not JSON, or nested past what can be read
                reply: dict[str, Any] | None = {"error": "malformed request"}
            else:
                try:
                    reply = await self._answer(msg, reader)
                except Exception as exc:  # a handler bug must not take the run down
                    reply = {"error": f"{type(exc).__name__}: {exc}"}
            if reply is None:
                return  # the asker hung up: no one is there to reply to
            writer.write(json.dumps(reply).encode("utf-8") + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            # the client went away or sent a line that is not JSON; it gets no reply
            pass
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(self, msg: Any, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """The reply to ``msg``; None when the asker of a question hangs up before it, which
        withdraws the question. Anything else is answered at once, so a step that came in
        is taken, also when its sender is gone."""
        if not (isinstance(msg, dict) and msg.get("op") == "approve"):
            return await self._dispatch(msg)
        answer = asyncio.ensure_future(self._dispatch(msg))
        self._asking.add(answer)
        gone = asyncio.ensure_future(_hung_up(reader))
        try:
            await asyncio.wait({answer, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not answer.done():
                return None
            return dict(WITHDRAWN) if answer.cancelled() else answer.result()
        finally:
            self._asking.discard(answer)
            for task in (answer, gone):
                task.cancel()
            await asyncio.gather(answer, gone, return_exceptions=True)


async def _hung_up(reader: asyncio.StreamReader) -> None:
    """Returns once the asker closes its end. A request is one line: more is let go."""
    with contextlib.suppress(ConnectionError):
        while await reader.read(65536):
            pass


async def request(path: Path | str, msg: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
    async def _go() -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_MESSAGE)
        try:
            writer.write(json.dumps(msg).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("the runner replied with something other than an object")
        return reply

    return await asyncio.wait_for(_go(), timeout)


def request_sync(path: Path | str, msg: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
    """The same request from a plain (non-async) process, such as the gate hook."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_MESSAGE:
                raise ValueError("reply too large")
    reply = json.loads(data)
    if not isinstance(reply, dict):
        raise ValueError("the runner replied with something other than an object")
    return reply
