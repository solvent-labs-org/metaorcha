"""The engines MetaBot can drive: the user's own coding-agent CLIs, plus any
chat-completions endpoint (a local model server, or a hosted one).

Engine ids are identifiers, not endorsements: ``claude``, ``codex``,
``cursor``, ``openai``.
"""

from __future__ import annotations

import importlib
import shutil
from typing import Any

ENGINES: dict[str, tuple[str, str]] = {
    "claude": ("metabot.engines.claude", "ClaudeEngine"),
    "codex": ("metabot.engines.codex", "CodexEngine"),
    "cursor": ("metabot.engines.cursor", "CursorEngine"),
    "openai": ("metabot.engines.openai_compat", "OpenAIEngine"),
}
BINARIES: dict[str, str] = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}


def get_engine(name: str) -> Any:
    try:
        module, cls = ENGINES[name]
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; choose one of: {', '.join(ENGINES)}") from None
    return getattr(importlib.import_module(module), cls)()


def available() -> dict[str, str | None]:
    """Each CLI engine's path on PATH, or None when it is not installed."""
    return {name: shutil.which(binary) for name, binary in BINARIES.items()}


def missing_command(engine: str) -> str | None:
    """The command an engine runs when it is not on PATH; None when it is, or it runs none."""
    binary = BINARIES.get(engine)
    if binary is None or shutil.which(binary) is not None:
        return None
    return binary
