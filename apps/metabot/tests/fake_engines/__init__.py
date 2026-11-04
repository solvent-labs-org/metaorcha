"""Test engines that drive tests/fake_engines/fake_cli.py instead of a real CLI."""

from __future__ import annotations

import sys
from typing import Any

from conftest import FAKE_CLI
from metabot.engines.base import CliEngine, EngineJob
from metabot.events import Event, Final, TextEvent


class FakeEngine(CliEngine):
    """Echoes ``{"type": "text"}`` and ``{"type": "final"}`` lines; nothing else."""

    name = "fake"
    binary = sys.executable
    tool_prefix = "fake"

    def argv(self, job: EngineJob) -> list[str]:
        return [sys.executable, str(FAKE_CLI), "--prompt", job.prompt]

    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        if kind == "text":
            return [TextEvent(str(obj.get("text", "")))]
        if kind == "final":
            return [Final(str(obj.get("text", "")), obj.get("session_id"))]
        if kind == "boom":
            raise RuntimeError("adapter bug")
        return []


def wrap(engine_cls: type[CliEngine]) -> CliEngine:
    """A real adapter whose binary is replaced by the fake CLI (argv shape kept)."""

    class Wrapped(engine_cls):  # type: ignore[valid-type, misc]  # base is the engine class passed in
        def argv(self, job: EngineJob) -> list[str]:
            real = super().argv(job)
            return [sys.executable, str(FAKE_CLI), *real[1:]]

    return Wrapped()
