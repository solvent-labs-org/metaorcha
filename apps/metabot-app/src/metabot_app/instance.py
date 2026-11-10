"""A6: one server per state folder.

``STATE/server.lock`` is held (fcntl, exclusive) for the server's lifetime and
``STATE/server.json`` records ``{pid, port, build}`` once the loopback listener is
bound, so a second ``serve`` finds the first (and says its port), the runner and
the Android shell read the port instead of guessing it, and a record left by a
dead process is never believed (its pid is checked).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

LOCK = "server.lock"
RECORD = "server.json"


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


class Instance:
    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self._fd = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self.state / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def record(self, port: int, build: str) -> None:
        path = self.state / RECORD
        tmp = path.with_suffix(".tmp")
        data = json.dumps({"pid": os.getpid(), "port": int(port), "build": str(build)}) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)

    def read(self) -> dict | None:
        try:
            data = json.loads((self.state / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("port"), int):
            return None
        return {"pid": data.get("pid"), "port": data["port"], "build": str(data.get("build") or "")}

    def running(self) -> dict | None:
        rec = self.read()
        return rec if rec and pid_alive(rec.get("pid")) else None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            (self.state / RECORD).unlink()
        except OSError:
            pass
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
