"""The user's MetaBot settings: ``<home>/config.json`` (0600).

Keys are never stored here. An endpoint for the ``openai`` engine that needs a
key names the environment variable that holds it (``openai_key_env``).

A file edited by hand into something MetaBot cannot read stops the command
that reads it, naming the file and what is wrong. It is never read as the
defaults: a setting the person chose, such as which engine a run goes to,
would be lost without a word.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from typing import Any
from urllib.parse import urlparse

from metabot.paths import metabot_home, write_private
from metabot.prices import read_prices

# A model name as an engine takes it: never empty, never a flag.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


@dataclass
class Config:
    default_engine: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    openai_base_url: str = "http://127.0.0.1:11434/v1"
    openai_model: str = "qwen2.5:3b"
    openai_key_env: str | None = None
    codex_ignore_user_config: bool = True
    sandbox: bool = True  # hide instructions outside the project from engine CLIs; metabot.sandbox
    fan_cap: int = 3  # desk runs at once; at most 5
    roster: dict[str, object] = field(default_factory=dict)  # role: engine, model; unchecked here
    project_receipts: bool = False  # also copy each receipt to <project>/.orcha/receipts/
    prices: dict[str, object] = field(default_factory=dict)  # model: dollars per million tokens
    ladder: list[object] = field(default_factory=list)  # rungs: engine, model, tier; metabot.ladder
    ladder_attempts: int = 3  # rungs one task may run; at most 5
    endpoints: dict[str, object] = field(
        default_factory=dict
    )  # name: base_url, model, key_env, tier
    ui_mode: str = "everyday"  # the home page: "everyday" (one box) or "developer" (the runs list)
    bridges: dict[str, object] = field(
        default_factory=dict
    )  # name: command, models, tools, read_only; metabot.engines.bridge


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    model: str | None
    key_env: str | None  # the variable that holds the key; None: the endpoint takes no key
    tier: str | None  # None: decided by the host


# An endpoint's name, and the variable its key is read from. A key value never matches the
# second: its letters are lower case, or it has a dash, or it is too long to be a name.
ENDPOINT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
ENDPOINT_KEYS = frozenset({"base_url", "model", "key_env", "tier"})
ENDPOINT_TIERS = ("local", "paid")
DEFAULT_ENDPOINT = "default"  # the openai_* settings, as one endpoint
UI_MODES = ("everyday", "developer")
BRIDGE_KEYS = frozenset({"command", "models", "tools", "read_only"})
BRIDGE_TOOLS = ("own", "switchboard")  # its own tools only, or MetaBot's switchboard too
BRIDGE_READ_ONLY = ("honored", "no")  # whether it keeps to a read-only job


def bridge_ok(name: Any, spec: Any) -> bool:
    """Whether ``name`` and ``spec`` make a bridge, in config.json or in a plugin."""
    from metabot.engines import ENGINES

    if not isinstance(name, str) or not ENDPOINT_NAME_RE.match(name) or name in ENGINES:
        return False
    if not isinstance(spec, dict) or set(spec) - BRIDGE_KEYS:
        return False
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        return False
    if not all(isinstance(w, str) and w and "\0" not in w for w in command):
        return False
    models = spec.get("models", [])
    if not isinstance(models, list) or not all(
        isinstance(m, str) and MODEL_RE.match(m) for m in models
    ):
        return False
    return (
        spec.get("tools", "own") in BRIDGE_TOOLS and spec.get("read_only", "no") in BRIDGE_READ_ONLY
    )


def _bridges(value: Any) -> bool:
    return isinstance(value, dict) and all(bridge_ok(n, s) for n, s in value.items())


def _endpoints(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for name, spec in value.items():
        if (
            not isinstance(name, str)
            or not ENDPOINT_NAME_RE.match(name)
            or name == DEFAULT_ENDPOINT
        ):
            return False
        if not isinstance(spec, dict) or set(spec) - ENDPOINT_KEYS:
            return False
        base = spec.get("base_url")
        if not isinstance(base, str):
            return False
        parts = urlparse(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        if "model" in spec and not (
            isinstance(spec["model"], str) and MODEL_RE.match(spec["model"])
        ):
            return False
        key_env = spec.get("key_env")
        if key_env is not None and not (isinstance(key_env, str) and KEY_ENV_RE.match(key_env)):
            return False
        if "tier" in spec and spec["tier"] not in ENDPOINT_TIERS:
            return False
    return True


def endpoint_of(cfg: Config, name: str) -> Endpoint:
    """The endpoint a rung names.

    ``default`` is the openai_* settings; any other name is a table entry.
    """
    if name == DEFAULT_ENDPOINT:
        return Endpoint(
            name, cfg.openai_base_url, cfg.openai_model or None, cfg.openai_key_env, None
        )
    spec = cfg.endpoints.get(name)
    if not isinstance(spec, dict):
        raise ConfigError(f"config.json has no endpoint named {name!r}")
    return Endpoint(
        name, spec["base_url"], spec.get("model"), spec.get("key_env"), spec.get("tier")
    )


class ConfigError(ValueError):
    """``config.json`` does not read. Every command reads it, so the command stops and names it."""


def _text_or_null(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _models(value: Any) -> bool:
    # a model reaches an engine's argv, so it is a model name, never a flag
    return isinstance(value, dict) and all(
        isinstance(m, str) and MODEL_RE.match(m) for m in value.values()
    )


def _prices(value: Any) -> bool:
    try:
        read_prices(value)
    except ValueError:
        return False
    return True


# What each setting is read as, and how an error names it. The roster is not
# here: a hand-edited roster that is not a table counts as none (metabot.roster).
# Nor is the ladder: the command that runs it reads it, and stops (metabot.ladder).
SETTING_TYPES: dict[str, tuple[Callable[[Any], bool], str]] = {
    "default_engine": (_text_or_null, "text or null"),
    "models": (_models, "a table of engine to model name"),
    "openai_base_url": (lambda v: isinstance(v, str), "text"),
    "openai_model": (lambda v: isinstance(v, str), "text"),
    "openai_key_env": (_text_or_null, "text or null"),
    "codex_ignore_user_config": (lambda v: isinstance(v, bool), "true or false"),
    "sandbox": (lambda v: isinstance(v, bool), "true or false"),
    "fan_cap": (lambda v: isinstance(v, int) and not isinstance(v, bool), "a whole number"),
    "project_receipts": (lambda v: isinstance(v, bool), "true or false"),
    "prices": (_prices, "a table of model to its in and out dollars per million tokens"),
    "ui_mode": (lambda v: v in UI_MODES, "everyday or developer"),
    "endpoints": (
        _endpoints,
        "a table of endpoint names to their base_url, model, key_env and tier; "
        "key_env is the name of an environment variable, never the key itself",
    ),
    "ladder_attempts": (
        lambda v: isinstance(v, int) and not isinstance(v, bool),
        "a whole number",
    ),
    "bridges": (
        _bridges,
        "a table of bridge names to their command (a list of words), models, tools "
        "(own or switchboard) and read_only (honored or no); a bridge never takes a "
        "built-in engine's name",
    ),
}


def _path():
    return metabot_home() / "config.json"


def configured_keys(cfg: Config) -> dict[str, str]:
    """``{VAR: key}`` for each key variable the settings name that the environment holds.

    The openai settings' variable and every endpoint's. A record takes each of them out,
    whichever endpoint a run called. Nothing is taken from the environment. A name no
    environment can hold, as one with a lone surrogate in it, holds no key: every run reads
    these, so such a name never stops one.
    """
    names = [cfg.openai_key_env]
    names += [spec.get("key_env") for spec in cfg.endpoints.values() if isinstance(spec, dict)]
    out: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        try:
            key = os.environ.get(name)
        except ValueError:  # the name does not encode for the environment
            continue
        if key:
            out[name] = key
    return out


def load_config() -> Config:
    """The settings, or the defaults when there is no file; ConfigError when it does not read.

    A key MetaBot does not know is left alone, so a file a newer version wrote still reads.
    """
    path = _path()
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"cannot read {path}: it holds no table of settings")
    for key, (fits, what) in SETTING_TYPES.items():
        if key in data and not fits(data[key]):
            raise ConfigError(f"cannot read {path}: {key} is not {what}")
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: Config) -> None:
    write_private(_path(), json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n")


def project_receipts_on(flag: bool | None = None) -> bool:
    """Whether a run copies its receipt into the project: the flag, else this machine's config."""
    return flag if flag is not None else load_config().project_receipts is True
