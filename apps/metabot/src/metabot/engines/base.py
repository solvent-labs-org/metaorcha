"""Run an engine's own CLI as a subprocess and turn its stream into events.

The prompt is one argv element and there is never a shell, so quotes,
newlines and ``$()`` reach the engine byte for byte. Stdin is closed (an
engine waiting on stdin would hang a headless run). The engine runs in its
own process group, so stopping a run stops everything the engine started.

The engine signs in with its own login; MetaBot never copies, passes on or
logs an engine's credentials. The ``claude`` engine opens the person's
settings file for a few named keys, and a key that file holds is never kept.
Variables that belong to an enclosing coding-agent
session are dropped so the child starts a session of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metabot.events import ErrorEvent, Event, Notice, TextEvent
from metabot.sandbox import SandboxPlan, named_file

DROPPED_ENV = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_EFFORT",
    }
)
STDERR_TAIL = 2000
KILL_GRACE = 5.0
DRAIN_GRACE = 2.0
EXIT_POLL = 0.05
MAX_LINE = 64 * 1024 * 1024
READ_CHUNK = 64 * 1024
NOT_FOUND = "not found; install it or check PATH"
NOT_EXECUTABLE = "is not executable"


@dataclass
class EngineJob:
    prompt: str
    cwd: Path
    model: str | None = None
    switchboard: list[str] | None = None  # argv that starts the switchboard MCP server
    plugin_dirs: list[Path] = field(default_factory=list)
    read_only: bool = False
    gate_hook: list[str] | None = None  # argv of the pre-tool-use gate, for engines with hooks
    sandbox: SandboxPlan | None = None  # what the run hides, and how; metabot.sandbox
    resume: str | None = None
    work_dir: Path | None = None  # a private directory the adapter may write to


class CliEngine(ABC):
    name: str
    binary: str
    tool_prefix: str
    skip_tools: tuple[str, ...] = ()
    supports_hooks: bool = False  # runs a pre-tool-use gate hook in headless mode
    loads_plugins: bool = False  # takes plugin folders itself; else skills come as tools
    reads_rules_above: bool = False  # reads every rule folder above its project, as cursor does
    read_only_mode: str  # how it takes a turn without writing
    also_sees: str  # what reaches it, besides the task and the project, that no run hides

    @property
    def own_tools_gated(self) -> bool:
        """Whether MetaBot's gate decides each of the engine's own tool calls: its hook does."""
        return self.supports_hooks

    def user_instructions(
        self, cwd: Path | None = None, *, copy_of: Path | None = None
    ) -> list[Path]:
        """The engine's own rules, skills or memory outside the project, that a run hides.

        ``copy_of``: ``cwd`` is where MetaBot makes a copy of the git project there."""
        return []

    def fills_as_it_starts(self) -> list[Path]:
        """Folders of those the engine fills as it starts, which a run makes to hide them."""
        return []

    def command_files(self) -> list[Path]:
        """The files the engine's command runs, which a run never hides."""
        return [p for p in [named_file(self.binary)] if p is not None]

    def prepare(self, job: EngineJob) -> EngineJob:
        """Write any files the engine needs for this job; return the job to run."""
        return job

    @abstractmethod
    def argv(self, job: EngineJob) -> list[str]:
        """The full command line; element 0 is the binary."""

    @abstractmethod
    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        """Events for one decoded stream object. ``state`` persists for the run."""

    def parse_line(self, line: str, state: dict[str, Any]) -> list[Event]:
        """Never raises: anything that is not a known event becomes text."""
        text = line.strip()
        if not text:
            return []
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            return [TextEvent(text)]
        if not isinstance(obj, dict):
            return [TextEvent(text)]
        try:
            return self.parse_event(obj, state)
        except Exception:  # an adapter that trips on a strange line must not end the run
            return [TextEvent(text)]

    def env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}


class _Proc:
    """One engine process: stdout read with a bounded drain, stderr tail kept."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        self.proc = proc
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.loop = asyncio.get_running_loop()
        self.exited = asyncio.ensure_future(self._poll_exit())
        self.tail = bytearray()
        self.stderr_task = asyncio.ensure_future(self._drain_stderr())
        self.pending: asyncio.Future[bytes] | None = None
        self.deadline: float | None = None

    async def _poll_exit(self) -> int:
        # Process.wait() also waits for every pipe to close, which never happens
        # while something the engine left behind still holds one: poll instead.
        while self.proc.returncode is None:
            await asyncio.sleep(EXIT_POLL)
        return self.proc.returncode

    async def _drain_stderr(self) -> None:
        while True:
            chunk = await self.stderr.read(READ_CHUNK)
            if not chunk:
                return
            self.tail += chunk
            if len(self.tail) > 4 * STDERR_TAIL:
                del self.tail[:-STDERR_TAIL]

    def _remaining(self) -> float:
        """Once the engine has exited, its pipes get one short grace in total."""
        if self.deadline is None:
            self.deadline = self.loop.time() + DRAIN_GRACE
        return max(0.0, self.deadline - self.loop.time())

    async def read(self) -> bytes:
        if self.pending is None:
            self.pending = asyncio.ensure_future(self.stdout.read(READ_CHUNK))
        if not self.exited.done():
            either: set[asyncio.Future[Any]] = {self.pending, self.exited}
            await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
        if not self.pending.done():
            done, _ = await asyncio.wait({self.pending}, timeout=self._remaining())
            if not done:  # something outlived the engine and still holds its stdout
                return b""
        chunk = self.pending.result()
        self.pending = None
        return chunk

    async def stderr_text(self) -> str:
        if not self.stderr_task.done():
            await asyncio.wait({self.stderr_task}, timeout=self._remaining())
        return bytes(self.tail[-STDERR_TAIL:]).decode("utf-8", errors="replace").strip()

    def _signal_group(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    async def _group_ends(self, grace: float) -> bool:
        """Whether every process in the group is gone within ``grace`` seconds."""
        deadline = self.loop.time() + grace
        while True:
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:  # one it may not signal is there, and stays
                return False
            if self.loop.time() >= deadline:
                return False
            await asyncio.sleep(EXIT_POLL)

    async def close(self) -> None:
        """Stop the engine and everything it started in its process group.

        The grace is the whole group's, not the first process's: under bwrap the
        first process is bwrap, which ends at the stop while the engine stops.
        """
        if self.proc.returncode is None:
            self._signal_group(signal.SIGTERM)
            if not await self._group_ends(KILL_GRACE):
                self._signal_group(signal.SIGKILL)
            await self.exited
        self._signal_group(signal.SIGTERM)  # whatever it left behind
        for task in (self.pending, self.stderr_task, self.exited):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Close our ends of the pipes even if a leftover still holds theirs.
        transport = getattr(self.proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


def _cannot_start(program: str, env: dict[str, str]) -> str | None:
    """Why ``program`` cannot start with ``env``, as starting it would say; None if it can."""
    if shutil.which(program, path=env.get("PATH", os.defpath)) is not None:
        return None
    return NOT_EXECUTABLE if "/" in program and os.path.exists(program) else NOT_FOUND


async def _lines(read: Callable[[], Awaitable[bytes]]) -> AsyncGenerator[bytes | None, None]:
    """Lines without the newline; ``None`` marks an over-long line that was dropped."""
    buf = bytearray()
    dropping = False
    while True:
        chunk = await read()
        if not chunk:
            if buf and not dropping:
                yield bytes(buf)
            return
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            if dropping:
                dropping = False
            elif nl > MAX_LINE:
                yield None
            else:
                yield bytes(buf[:nl])
            del buf[: nl + 1]
        if len(buf) > MAX_LINE and not dropping:
            dropping = True
            buf.clear()
            yield None
        elif dropping:
            buf.clear()


async def run_cli(
    engine: CliEngine, job: EngineJob, on_stop: Callable[[], None] | None = None
) -> AsyncGenerator[Event, None]:
    """Events from one engine run. Use under ``contextlib.aclosing``.

    ``on_stop`` is called as the run ends, before the engine is stopped: when it was cut
    off, the engine may still be waiting on a question to the person.
    """
    if not Path(job.cwd).is_dir():
        yield ErrorEvent(f"{engine.name}: working directory {job.cwd} does not exist", True)
        return
    try:
        job = engine.prepare(job)
    except (OSError, ValueError) as exc:
        yield ErrorEvent(f"{engine.name}: could not prepare the run: {exc}", True)
        return
    argv = engine.argv(job)
    env = engine.env()
    if job.sandbox is not None:
        if job.sandbox.refused:
            yield ErrorEvent(job.sandbox.refused, True)
            return
        # bwrap would start and fail to start the engine: say so as a start would
        why = _cannot_start(argv[0], env) if job.sandbox.tool else None
        if why:
            yield ErrorEvent(f"{engine.name}: {argv[0]} {why}", True)
            return
        note = job.sandbox.note()
        if note:
            yield Notice(note)
        argv = job.sandbox.wrap(argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(job.cwd),
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        yield ErrorEvent(f"{engine.name}: {argv[0]} {NOT_FOUND}", True)
        return
    except PermissionError:
        yield ErrorEvent(f"{engine.name}: {argv[0]} {NOT_EXECUTABLE}", True)
        return
    if job.sandbox is not None:
        job.sandbox.started = True

    running = _Proc(proc)
    state: dict[str, Any] = {}
    try:
        async with contextlib.aclosing(_lines(running.read)) as lines:
            async for raw in lines:
                if raw is None:
                    yield TextEvent(f"[metabot: a line over {MAX_LINE} bytes was dropped]")
                    continue
                for event in engine.parse_line(raw.decode("utf-8", errors="replace"), state):
                    yield event
        rc = await running.exited
        tail = await running.stderr_text()
        if rc != 0:
            message = f"{engine.name} exited {rc}"
            yield ErrorEvent(f"{message}: {tail}" if tail else message)
    finally:
        if on_stop is not None:
            on_stop()
        await running.close()
