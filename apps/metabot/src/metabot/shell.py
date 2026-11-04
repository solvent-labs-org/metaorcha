"""One shell command in its own process group, stopped whole on a timeout or a cancel.

The local engine's shell tool and an office's gate checks both run here.
A command runs under ``/bin/sh -c`` with no input, in a new session, so its
process group is everything it started. A timeout or a cancel stops that
whole group: a polite signal first, then a kill. Output is read as it comes
and only its tail is kept, so a command that prints without end cannot
fill the memory of the process that runs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path

STOP_GRACE_S = 2.0
READ_CHUNK = 65536
TIMEOUT_EXIT = 124  # what timeout(1) reports for a command it had to stop


@dataclass(frozen=True)
class ShellResult:
    exit_code: int | None  # None when it timed out; 128 + N when signal N ended it
    stdout: str  # the tail, at most the limit asked for
    stderr: str  # the tail; empty when stderr went to stdout
    timed_out: bool
    pgid: int
    left_running: bool  # something it started in the background is still running


def alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def stop_group(pgid: int) -> None:
    """Stop a whole process group: a polite signal, a short wait, then a kill."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + STOP_GRACE_S
    while alive(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)  # whatever ignored the first signal


async def _stop(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), STOP_GRACE_S)


async def _tail(stream: asyncio.StreamReader | None, kept: bytearray, limit: int) -> None:
    """Read a stream to its end into ``kept``, keeping only the last ``limit`` bytes."""
    if stream is None:
        return
    while chunk := await stream.read(READ_CHUNK):
        kept += chunk
        if len(kept) > limit:
            del kept[: len(kept) - limit]


def _exit_code(returncode: int | None) -> int | None:
    if returncode is None:
        return None
    return 128 - returncode if returncode < 0 else returncode


async def run_shell(
    command: str,
    cwd: Path,
    timeout: float,
    *,
    limit: int = 20_000,
    merge_stderr: bool = False,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """Run one command; a timeout or a cancel stops everything it started."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out, err = bytearray(), bytearray()
    reading = asyncio.gather(
        _tail(proc.stdout, out, limit), _tail(proc.stderr, err, limit), proc.wait()
    )
    timed_out = False
    try:
        await asyncio.wait_for(reading, timeout)
    except TimeoutError:
        timed_out = True
        await _stop(proc)
    except BaseException:  # cancelled: nothing it started outlives the caller
        reading.cancel()
        await _stop(proc)
        raise
    return ShellResult(
        exit_code=None if timed_out else _exit_code(proc.returncode),
        stdout=out.decode("utf-8", errors="replace"),  # what came before a timeout, too
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        pgid=proc.pid,
        left_running=not timed_out and alive(proc.pid),
    )
