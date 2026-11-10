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


def with_login(engine: Any, name: str | None) -> Any:
    """``engine``, to run on the login ``name`` (S3); unchanged for None or ``default``.

    The login must be the engine's own, and its folder must still be fit to be one: there,
    the person's, owner-only, and outside every project and MetaBot's own home, looked at
    with ``stat`` alone. Anything else refuses the run before it starts.

    A variable set here that would sign the engine in another way (S4) refuses the run on a
    login that refuses, the default, naming each variable and the command that changes the
    choice; a login that clears drops them for the run and says how many in ``cleared``.
    """
    if name is None:
        return engine
    from metabot.config import LOGIN_ENGINES, load_config, login_of
    from metabot.logins import run_problems

    engine_name = getattr(engine, "name", "?")
    takes = engine_name in LOGIN_ENGINES and getattr(engine, "home_variable", None) is not None
    login = login_of(load_config(), name)  # an unknown name is refused here
    if login is None:
        return engine
    if not takes:
        raise ValueError(
            f"{engine_name} takes no login: logins are for {' and '.join(LOGIN_ENGINES)}"
        )
    if login.engine != engine_name:
        raise ValueError(f"{login.name} is a {login.engine} login, not one for {engine_name}")
    problems = run_problems(login.home)
    if problems:
        raise ValueError(f"login {login.name} cannot be used: " + "; ".join(problems))
    present = engine.sign_in_present()
    if present and login.on_sign_in != "clear":
        names = ", ".join(present)
        it, verb = ("it", "is") if len(present) == 1 else ("them", "are")
        raise ValueError(
            f"login {login.name}: {names} {verb} set here, and would sign {engine_name} in "
            f"instead of the login. Unset {it}, or let MetaBot clear {it} for this login's "
            f"runs: metabot login set {login.name} --on-sign-in clear"
        )
    engine.cleared = present
    engine.login = login
    return engine


def get_engine(name: str, login: str | None = None) -> Any:
    """The engine ``name``, on the login ``login`` when one is named (see ``with_login``)."""
    if login is not None:
        return with_login(get_engine(name), login)
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
