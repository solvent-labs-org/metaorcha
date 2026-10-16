"""The engine-neutral event stream every adapter produces.

An adapter turns one line of its engine's output into zero or more of these.
The runner, the step recorder and the console only ever see these types, so
a new engine is one parser and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Started:
    session_id: str | None
    model: str | None


@dataclass(frozen=True)
class TextEvent:
    text: str


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: Any
    ok: bool


@dataclass(frozen=True)
class Usage:
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Final:
    text: str
    session_id: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    message: str


@dataclass(frozen=True)
class Notice:
    """Something the engine reported that does not fail the run (a retry, a warning)."""

    text: str


Event = Started | TextEvent | ToolCall | ToolResult | Usage | Final | ErrorEvent | Notice
