"""The runner's private channel: one Unix socket per run, owner-only.

Newline-delimited JSON, one request per connection. The switchboard and the
gate hook run as children of the engine, so they cannot write the run's
journal themselves; they send the runner approvals to decide and steps to
record, and the runner stays the only writer.

Ops: ``{"op": "approve", ...}`` -> ``{"allow": bool, "reason": str}``;
``{"op": "step", "step": {...}}`` -> ``{"ok": true}``; anything else ->
``{"error": "unknown op"}``.
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


def socket_path(run_dir: Path) -> Path:
    """The run's socket path; a private temp dir when the run dir's path is too long."""
    candidate = run_dir / "control.sock"
    if len(os.fsencode(str(candidate))) <= SUN_PATH_LIMIT:
        return candidate
    return Path(tempfile.mkdtemp(prefix="mb-")) / "control.sock"  # mkdtemp is 0700


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
        self._closing = False

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(self.path), limit=MAX_MESSAGE
            )
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)

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
                reply = await self._dispatch(json.loads(line))
            except ValueError:
                reply = {"error": "malformed request"}
            except Exception as exc:  # a handler bug must not take the run down
                reply = {"error": f"{type(exc).__name__}: {exc}"}
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
