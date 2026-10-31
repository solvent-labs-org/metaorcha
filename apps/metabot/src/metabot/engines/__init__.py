"""The engines MetaBot can drive: the user's own coding-agent CLIs, plus any
chat-completions endpoint (a local model server, or a hosted one).

Engine ids are identifiers, not endorsements: ``claude``, ``codex``,
``cursor``, ``openai``.
"""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path
from typing import Any, TypeGuard

ENGINES: dict[str, tuple[str, str]] = {
    "claude": ("metabot.engines.claude", "ClaudeEngine"),
    "codex": ("metabot.engines.codex", "CodexEngine"),
    "cursor": ("metabot.engines.cursor", "CursorEngine"),
    "openai": ("metabot.engines.openai_compat", "OpenAIEngine"),
}
BINARIES: dict[str, str] = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}


def bridge_table() -> dict[str, tuple[dict[str, Any], Any]]:
    """Every bridge by name, with the enabled plugin it comes from, or None for config.json.

    This machine's settings come first, so a plugin's bridge never takes a name
    they use. Settings that do not read, or plugin records that do not, give
    no bridges; a run that takes the plugins refuses them with the reason.
    """
    from metabot.config import ConfigError, load_config
    from metabot.plugins import PluginError, enabled_bridges

    try:
        table = load_config().bridges
    except ConfigError:
        table = {}
    found: dict[str, tuple[dict[str, Any], Any]] = (
        {k: (v, None) for k, v in table.items() if isinstance(v, dict)}
        if isinstance(table, dict)
        else {}
    )
    try:
        from_plugins = enabled_bridges()
    except PluginError:
        from_plugins = {}
    for name, (plugin, spec) in from_plugins.items():
        found.setdefault(name, (spec, plugin))
    return found


def bridge_specs() -> dict[str, dict[str, Any]]:
    """The bridges a run may name: this machine's settings', then enabled plugins'."""
    return {name: spec for name, (spec, _plugin) in bridge_table().items()}


def engine_names() -> list[str]:
    """Every engine a run may name: the built-in ones, then the bridges."""
    return [*ENGINES, *(name for name in bridge_specs() if name not in ENGINES)]


def known_engine(name: object) -> TypeGuard[str]:
    return isinstance(name, str) and (name in ENGINES or name in bridge_specs())


def is_bridge(name: object) -> bool:
    return isinstance(name, str) and name not in ENGINES and name in bridge_specs()


def bridge_keeps_read_only(name: str) -> bool:
    """Whether a bridge says it keeps to a read-only job; a built-in engine always does."""
    if name in ENGINES:
        return True
    return bridge_specs().get(name, {}).get("read_only") == "honored"


def get_engine(name: str) -> Any:
    if name in ENGINES:
        module, cls = ENGINES[name]
        return getattr(importlib.import_module(module), cls)()
    found = bridge_table().get(name)
    if found is None:
        raise ValueError(f"unknown engine {name!r}; choose one of: {', '.join(engine_names())}")
    from metabot.engines.bridge import BridgeEngine

    spec, plugin = found
    if plugin is None:
        return BridgeEngine(name, spec)
    from metabot.plugins import PluginError, bridge_refused, verify_installed

    verify_installed(plugin)  # what the bridge runs is the copy its hash names
    said = bridge_refused(name, spec["command"])
    if said is not None:
        raise PluginError(said)
    return BridgeEngine(name, spec, source=f"plugin {plugin.pin}")


def available() -> dict[str, str | None]:
    """Each CLI engine's path on PATH, or None when it is not installed."""
    return {name: shutil.which(binary) for name, binary in BINARIES.items()}


def missing_command(engine: str) -> str | None:
    """The command an engine runs when it is not on PATH; None when it is, or it runs none."""
    spec = None if engine in ENGINES else bridge_specs().get(engine)
    if spec is not None:
        word = str(spec["command"][0])
        found = shutil.which(word) if "/" not in word else (word if Path(word).is_file() else None)
        return None if found else word
    binary = BINARIES.get(engine)
    if binary is None or shutil.which(binary) is not None:
        return None
    return binary
