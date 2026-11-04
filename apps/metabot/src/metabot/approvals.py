"""Who answers when the gate asks.

A write is approved with ``y``; a destructive call only with the exact word
``yes``. Prompts go to the controlling terminal, never to stdout, so a piped
run cannot be answered by its own input. Anything typed before a prompt
appears is discarded, so type-ahead never answers one. With no terminal,
nobody can approve: every ask is declined (and recorded by the caller).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import select
import termios
import threading
from dataclasses import dataclass, field, replace
from typing import Protocol

import anyio

from metabot.scope_classes import ScopeClass

PREVIEW_LIMIT = 400
STOP_POLL_S = 0.2
CLOSE_WAIT_S = 1.0  # a closed question's thread ends within STOP_POLL_S


@dataclass(frozen=True)
class ApprovalRequest:
    call_id: str
    tool: str
    cls: ScopeClass
    preview: str
    who: str = ""  # which run asks, when several share one terminal


class Approver(Protocol):
    kind: str

    async def ask(self, req: ApprovalRequest) -> bool:
        """Whether the person allows this call."""


def printable(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """Escape control characters so a tool argument cannot repaint the prompt."""
    out = []
    for ch in text[:limit]:
        if ch == "\n":
            out.append("\\n")
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(ch.encode("unicode_escape").decode("ascii"))
    if len(text) > limit:
        out.append("…")
    return "".join(out)


# C0 and C1 controls except tab and newline, the marks that reorder text, and half of a pair
# standing alone, as Python reads a byte of a name that is not UTF-8: it cannot be written.
_UNSAFE_POINTS = (
    *range(0x00, 0x09),
    *range(0x0B, 0x20),
    *range(0x7F, 0xA0),
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
    *range(0xD800, 0xE000),
)
_UNSAFE = re.compile("[" + "".join(re.escape(chr(c)) for c in _UNSAFE_POINTS) + "]")


def terminal_safe(text: str) -> str:
    """Long text, such as an answer, for a terminal: whole, with lines and tabs kept.

    Every other control character and every bidi mark is escaped, so the text
    cannot set a window title, clear the screen, write over a line or reorder
    what the person reads.
    """
    return _UNSAFE.sub(
        lambda m: m.group().encode("unicode_escape").decode("ascii"),
        text.replace("\r\n", "\n"),
    )


def _open_tty(path: str) -> int:
    """A raw read-write descriptor on the terminal (text mode cannot open a tty r+)."""
    return os.open(path, os.O_RDWR | os.O_NOCTTY)


def _read_line(fd: int, stop: threading.Event | None = None, limit: int = 4096) -> str:
    """One line from the terminal; returns early (empty) once ``stop`` is set."""
    data = b""
    while b"\n" not in data and len(data) < limit:
        if stop is not None:
            ready, _, _ = select.select([fd], [], [], STOP_POLL_S)
            if stop.is_set():
                return ""
            if not ready:
                continue
        chunk = os.read(fd, limit)
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")


class TtyApprover:
    kind = "human"

    def __init__(self, tty_path: str = "/dev/tty") -> None:
        self.tty_path = tty_path
        self._lock = asyncio.Lock()

    def _ask_sync(self, req: ApprovalRequest, stop: threading.Event | None = None) -> bool:
        label = req.cls.label
        word = "yes" if req.cls is ScopeClass.DESTRUCTIVE else "y"
        who = f"{printable(req.who, 64)}: " if req.who else ""
        prompt = (
            f"\n[metabot] {who}{label} call wants to run\n"
            f"  tool: {printable(req.tool, 200)}\n"
            f"  args: {printable(req.preview)}\n"
            f"  allow? type {word!r} to allow, anything else declines: "
        )
        fd = _open_tty(self.tty_path)
        try:
            with contextlib.suppress(termios.error):
                termios.tcflush(fd, termios.TCIFLUSH)  # type-ahead never answers
            os.write(fd, prompt.encode("utf-8"))
            answer = _read_line(fd, stop).strip().lower()
            if stop is not None and stop.is_set():
                closed = f"\n[metabot] {who}cancelled; declined: {printable(req.tool, 200)}\n"
                with contextlib.suppress(OSError):
                    os.write(fd, closed.encode("utf-8"))
                return False
        finally:
            os.close(fd)
        if req.cls is ScopeClass.DESTRUCTIVE:
            return answer == "yes"
        return answer in ("y", "yes")

    async def ask(self, req: ApprovalRequest) -> bool:
        async with self._lock:
            stop = threading.Event()
            # A future, not a task, which a stopping loop would cancel: the question ends, and
            # says so, before anything else is put to the person.
            asked = asyncio.get_running_loop().run_in_executor(None, self._ask_sync, req, stop)
            try:
                return await asyncio.shield(asked)
            finally:
                stop.set()  # a cancelled ask lets go of the terminal at once,
                await _closed(asked)  # and says so before the next is put


async def _closed(asked: asyncio.Future[bool]) -> None:
    """Wait for a question's thread to end, however often the ask is stopped meanwhile: a
    run's stop, and then its engine hanging up, each take it back. A terminal that takes no
    output, stopped with Ctrl-S, is waited on for CLOSE_WAIT_S at most."""
    loop = asyncio.get_running_loop()
    end = loop.time() + CLOSE_WAIT_S
    with anyio.CancelScope(shield=True):  # a task group's stop, said on every turn of the loop
        while not asked.done() and (left := end - loop.time()) > 0:
            with contextlib.suppress(asyncio.CancelledError):  # and a task's, said again
                await asyncio.wait({asked}, timeout=left)


class Labelled:
    """An approver that names the run asking, for runs that share one terminal."""

    def __init__(self, inner: Approver, who: str) -> None:
        self.inner = inner
        self.who = who
        self.kind = inner.kind

    async def ask(self, req: ApprovalRequest) -> bool:
        return await self.inner.ask(replace(req, who=self.who))


class DenyAll:
    kind = "no-human"

    async def ask(self, req: ApprovalRequest) -> bool:
        return False


@dataclass
class Scripted:
    answers: list[bool]
    asked: int = 0
    requests: list[ApprovalRequest] = field(default_factory=list)
    kind: str = "human"

    async def ask(self, req: ApprovalRequest) -> bool:
        self.asked += 1
        self.requests.append(req)
        return self.answers.pop(0) if self.answers else False


def pick_approver() -> Approver:
    """A terminal approver when this process has a controlling terminal."""
    try:
        os.close(_open_tty("/dev/tty"))
    except OSError:
        return DenyAll()
    return TtyApprover()
