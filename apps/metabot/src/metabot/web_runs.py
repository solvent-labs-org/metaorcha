"""Runs started in ``metabot ui``, and the asks they wait on.

A run started in the page is a task in the UI's event loop, with the page's
approver. When the gate asks about a call, the approver files an ask and
waits for the person to answer it on the run's live page. A write is
allowed with Allow; a destructive call only with the exact word ``yes``, as
in the terminal. An ask nobody answers in time is declined. The gate signs
each answer into the run's verdicts, as it does a terminal answer.

An office run started in the page is one task too. Its desks run on their
own engines, its asks name the desk that asks, and its record, not one
receipt, says what each desk did.

An answered or expired ask is kept a while, so a late answer is told it
came too late. Closing the UI stops every run: each pending ask is
declined and each run is sealed ``cancelled``.
"""

from __future__ import annotations

import asyncio
import io
import secrets
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

from metabot.approvals import ApprovalRequest, printable
from metabot.console import Trace
from metabot.scope_classes import ScopeClass

ASK_KEEP_S = 600.0  # an answered or expired ask is kept this long: a late answer gets 410
EVENT_LIMIT = 2000  # lines of a run's trace kept for its live page
YES = "yes"


class AskExpired(Exception):
    """The ask is no longer waiting: its time ran out, or its run ended."""


class AskAnswered(Exception):
    """The ask was answered already; the first answer stands."""


@dataclass
class Ask:
    ask_id: str
    run_id: str
    request: ApprovalRequest
    future: asyncio.Future[bool]
    state: str = "waiting"  # then allowed, declined, expired, or ended
    closed_at: float | None = None

    def close(self, state: str) -> None:
        self.state = state
        self.closed_at = time.monotonic()


class _Lines(io.TextIOBase):
    """What a trace prints, kept as lines; past the limit the oldest go."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.dropped = 0
        self._partial = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._partial += text
        *whole, self._partial = self._partial.split("\n")
        self.lines.extend(whole)
        extra = len(self.lines) - EVENT_LIMIT
        if extra > 0:
            del self.lines[:extra]
            self.dropped += extra
        return len(text)


@dataclass
class LiveRun:
    """A run started in the page: what it runs, what it said, and how it ended.

    An office run has no engine of its own: each desk runs on its own.
    """

    run_id: str
    engine: str
    model: str | None
    cwd: str
    prompt: str
    office: str | None = None  # an office run: the office's name
    mode: str = ""  # an office run: relay or fan
    desks: tuple[str, ...] = ()  # an office run: the desks chosen, in order
    out: _Lines = field(default_factory=_Lines)
    task: asyncio.Task[None] | None = None
    state: str = "running"  # then done, stopped, or failed
    result: Any = None  # the sealed run's RunResult, or the office run's OfficeResult
    error: str | None = None

    def __post_init__(self) -> None:
        self.trace = Trace(out=self.out)


class Broker:
    """Every run this UI started, and every ask they filed."""

    def __init__(self, ask_timeout_s: float = 600.0) -> None:
        self.ask_timeout_s = ask_timeout_s
        self.runs: dict[str, LiveRun] = {}
        self.asks: dict[str, Ask] = {}

    # ── runs ────────────────────────────────────────────────────────────────

    def open(
        self,
        run_id: str,
        *,
        engine: str = "",
        model: str | None = None,
        cwd: str,
        prompt: str,
        office: str | None = None,
        mode: str = "",
        desks: Sequence[str] = (),
    ) -> LiveRun:
        if run_id in self.runs:  # a second run under one id would leave the first unreachable
            raise ValueError(f"run {run_id} is open already")
        live = LiveRun(run_id, engine, model, cwd, prompt, office, mode, tuple(desks))
        self.runs[run_id] = live
        return live

    def start(self, run_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Run an opened run's coroutine as a task in this loop."""
        live = self.runs[run_id]
        live.task = asyncio.get_running_loop().create_task(self._run(live, coro))

    async def _run(self, live: LiveRun, coro: Coroutine[Any, Any, Any]) -> None:
        from metabot.office import OfficeError
        from metabot.runner import RunCancelled

        try:
            live.result = await coro
            live.state = "done"
        except RunCancelled as exc:  # stopped: the receipt was still sealed
            live.state = "stopped"
            if live.office is None:  # an office's record says what each desk did
                live.result = exc.result
        except asyncio.CancelledError:
            live.state = "stopped"
            if live.office is None:  # a run stopped here had not begun: nothing was sealed
                live.error = "stopped before the run began"
        except OfficeError as exc:  # the office run could not open: it says why
            live.state, live.error = "failed", str(exc)
        except Exception as exc:  # the engine could not start, or the run broke
            live.state, live.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            for ask in self.pending(live.run_id):
                ask.close("ended")

    def events(self, run_id: str, after: int = 0) -> list[str]:
        """The run's trace lines from line ``after`` on; KeyError for a run not started here."""
        out = self.runs[run_id].out
        return out.lines[max(0, after - out.dropped) :]

    def stop(self, run_id: str) -> None:
        """Stop a run; it is sealed ``cancelled``. KeyError for a run not started here."""
        task = self.runs[run_id].task
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Stop every run and wait until each is sealed."""
        tasks = [live.task for live in self.runs.values() if live.task and not live.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── asks ────────────────────────────────────────────────────────────────

    def pending(self, run_id: str) -> list[Ask]:
        return [a for a in self.asks.values() if a.run_id == run_id and a.state == "waiting"]

    def _prune(self) -> None:
        now = time.monotonic()
        for ask_id in [
            k for k, a in self.asks.items() if a.closed_at and now - a.closed_at > ASK_KEEP_S
        ]:
            del self.asks[ask_id]

    def _say(self, run_id: str, text: str) -> None:
        live = self.runs.get(run_id)
        if live is not None:
            live.trace.line(text)

    async def ask(self, run_id: str, req: ApprovalRequest) -> bool:
        """File an ask and wait for its answer; declined when nobody answers in time."""
        self._prune()
        loop = asyncio.get_running_loop()
        ask = Ask(f"ask-{secrets.token_hex(8)}", run_id, req, loop.create_future())
        self.asks[ask.ask_id] = ask
        tool = printable(req.tool, 120)
        said = f"{_who(req)}{req.cls.label} call {tool} waits for you ({ask.ask_id})"
        self._say(run_id, f"[metabot] {said}")
        try:
            await asyncio.wait({ask.future}, timeout=self.ask_timeout_s)
        except asyncio.CancelledError:  # the run is stopping: the gate records the decline
            if ask.state == "waiting":
                ask.close("ended")
            raise
        if ask.future.done():  # answered, also in the moment the time ran out
            return ask.future.result()
        ask.close("expired")
        self._say(run_id, f"[metabot] {_who(req)}no answer in time: {tool} declined")
        return False

    def answer(self, ask_id: str, allow: bool, word: str = "") -> Ask:
        """Answer a waiting ask.

        KeyError for an unknown ask, AskAnswered for one already answered,
        AskExpired for one no longer waiting, and ValueError for a
        destructive call allowed without the word ``yes``; the ask then
        still waits.
        """
        self._prune()
        ask = self.asks[ask_id]
        if ask.state in ("allowed", "declined"):
            raise AskAnswered(ask.state)
        if ask.state != "waiting":
            raise AskExpired(ask.state)
        if allow and ask.request.cls is ScopeClass.DESTRUCTIVE and word.strip().lower() != YES:
            raise ValueError("a destructive call is allowed only when you type the word yes")
        ask.close("allowed" if allow else "declined")
        ask.future.set_result(allow)
        verb = "allowed" if allow else "declined"
        tool = printable(ask.request.tool, 120)
        self._say(ask.run_id, f"[metabot] {_who(ask.request)}{tool} {verb} in the page")
        return ask


def _who(req: ApprovalRequest) -> str:
    """Which desk asks, when an office run's desk does."""
    return f"{printable(req.who, 64)}: " if req.who else ""


class WebApprover:
    """Answers the gate from the page: each ask waits on the run's live page."""

    kind = "human"

    def __init__(self, broker: Broker, run_id: str) -> None:
        self.broker = broker
        self.run_id = run_id

    async def ask(self, req: ApprovalRequest) -> bool:
        return await self.broker.ask(self.run_id, req)
