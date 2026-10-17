"""What a person sees while a run goes: a short trace on stderr.

The answer itself goes to stdout, so ``metabot ask ... > answer.txt`` keeps
only the answer. ``--quiet`` hides the trace but never an error. Text from
engines and tools is escaped before it is shown.
"""

from __future__ import annotations

import json
import sys
from io import TextIOBase
from typing import Any, TextIO

from metabot.approvals import printable
from metabot.events import ErrorEvent, Event, Notice, Started, ToolCall, ToolResult

ARG_PREVIEW = 120


def _args_preview(args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "query", "url"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return printable(value, ARG_PREVIEW)
    return printable(json.dumps(args, ensure_ascii=False), ARG_PREVIEW) if args else ""


def _one_line(text: str) -> str:
    """A tool's error on one line: its lines in order, the blank ones dropped."""
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


class Trace:
    def __init__(
        self, out: TextIO | TextIOBase | None = None, quiet: bool = False, label: str | None = None
    ) -> None:
        self.out = out or sys.stderr
        self.quiet = quiet
        self.label = printable(label, 40) if label else None
        self.tools: dict[str, str] = {}

    def for_run(self, label: str) -> Trace:
        """A trace for one of several runs going at once: each of its lines names the run."""
        return Trace(self.out, self.quiet, label)

    def line(self, text: str) -> None:
        if self.label:
            text = f"{self.label}| {text}"
        print(text, file=self.out, flush=True)

    def note(self, text: str) -> None:
        """A line that ``--quiet`` hides (errors use :meth:`line`)."""
        if not self.quiet:
            self.line(text)

    def __call__(self, event: Event) -> None:
        if isinstance(event, ErrorEvent):
            self.line(f"[metabot] {printable(event.message, 600)}")
            return
        if self.quiet:
            return
        if isinstance(event, Started):
            model = f" ({printable(event.model, 80)})" if event.model else ""
            self.line(f"[metabot] started{model}")
        elif isinstance(event, ToolCall):
            name = event.tool.removeprefix("mcp__metabot__")
            self.tools[event.call_id] = name
            self.line(f"  - {printable(name, 60)}  {_args_preview(event.args)}".rstrip())
        elif isinstance(event, ToolResult) and not event.ok:
            name = self.tools.get(event.call_id, "call")
            detail = event.output if isinstance(event.output, str) else json.dumps(event.output)
            said = printable(_one_line(detail), 160)
            self.line(f"    {printable(name, 60)} returned an error: {said}")
        elif isinstance(event, Notice):
            self.line(f"[metabot] {printable(event.text, 300)}")
