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

from metabot.scope_classes import ScopeClass

PREVIEW_LIMIT = 400
STOP_POLL_S = 0.2


@dataclass(frozen=True)
class ApprovalRequest:
    call_id: str
    tool: str
    cls: ScopeClass
    preview: str
    who: str = ""  # which run asks, when several share one terminal


class Approver(Protocol):
    kind: str

    async def ask(self, req: ApprovalRequest) -> bool: ...


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


# C0 and C1 controls except tab and newline, and the marks that reorder text.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


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
                with contextlib.suppress(OSError):
                    os.write(fd, b"\n[metabot] cancelled; declined\n")
                return False
        finally:
            os.close(fd)
        if req.cls is ScopeClass.DESTRUCTIVE:
            return answer == "yes"
        return answer in ("y", "yes")

    async def ask(self, req: ApprovalRequest) -> bool:
        async with self._lock:
            stop = threading.Event()
            try:
                return await asyncio.to_thread(self._ask_sync, req, stop)
            finally:
                stop.set()  # a cancelled ask lets go of the terminal at once


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
