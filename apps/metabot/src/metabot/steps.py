"""Turn an engine's tool calls into the raw steps a receipt seals.

A call and its result are paired by id; latency is measured between them on
this machine's monotonic clock. A call that never got a result (the engine
died, or the run was cut short) is still a step: failed, output
``{"error": "no result"}``, or, from an engine whose own tools pass no gate,
``{"error": "no result; the engine may have run it"}``: such an engine can
run a call before it says so. Nothing an engine did is dropped silently.

Steps from the switchboard arrive already formed (``add_external``) and keep
their arrival order among the engine's own calls.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from metabot.events import Event, ToolCall, ToolResult

_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")
ID_LIMIT = 200
NO_RESULT = {"error": "no result"}
MAY_HAVE_RUN = {"error": "no result; the engine may have run it"}


def ascii_text(value: Any, fallback: str = "?", limit: int = ID_LIMIT) -> str:
    """Printable ASCII for an envelope text field (the RFC charset rule)."""
    text = _NOT_PRINTABLE.sub("_", str(value))[:limit]
    return text or fallback


@dataclass
class _Pending:
    order: int
    step: dict[str, Any]
    started: float


class StepRecorder:
    def __init__(
        self,
        prefix: str,
        skip_tools: tuple[str, ...] = (),
        unanswered: dict[str, Any] | None = None,
    ) -> None:
        self.prefix = prefix
        self.skip_tools = skip_tools
        self.unanswered = dict(NO_RESULT if unanswered is None else unanswered)
        self._order = 0
        self._open: dict[str, _Pending] = {}
        self._done: list[tuple[int, dict[str, Any]]] = []

    def _next(self) -> int:
        self._order += 1
        return self._order

    def _tool_name(self, tool: str) -> str:
        name = tool if "#" in tool else f"{self.prefix}/{tool}"
        return ascii_text(name)

    def on_event(self, event: Event) -> None:
        if isinstance(event, ToolCall):
            if event.tool.startswith(self.skip_tools):
                return
            earlier = self._open.pop(event.call_id, None)
            if earlier is not None:  # a reused id: the first call never got its result
                self._done.append((earlier.order, earlier.step))
            args = event.args if isinstance(event.args, dict) else {"value": event.args}
            step = {
                "call_id": ascii_text(event.call_id),
                "tool": self._tool_name(event.tool),
                "args": args,
                "output": dict(self.unanswered),
                "success": False,
                "latency_ms": 0,
            }
            self._open[event.call_id] = _Pending(self._next(), step, time.monotonic())
        elif isinstance(event, ToolResult):
            pending = self._open.pop(event.call_id, None)
            if pending is None:
                return
            step = pending.step
            step["output"] = event.output
            step["success"] = bool(event.ok)
            step["latency_ms"] = max(0, int((time.monotonic() - pending.started) * 1000))
            self._done.append((pending.order, step))

    def add_external(self, step: dict[str, Any]) -> None:
        self._done.append((self._next(), dict(step)))

    def steps(self) -> list[dict[str, Any]]:
        unfinished = [(p.order, dict(p.step)) for p in self._open.values()]
        return [step for _, step in sorted(self._done + unfinished, key=lambda x: x[0])]
