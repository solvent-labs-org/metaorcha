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
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

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
    fan_cap: int = 3  # desk runs at once; at most 5
    roster: dict[str, object] = field(default_factory=dict)  # role: engine, model; unchecked here
    project_receipts: bool = False  # also copy each receipt to <project>/.orcha/receipts/
    prices: dict[str, object] = field(default_factory=dict)  # model: dollars per million tokens
    ladder: list[object] = field(default_factory=list)  # rungs: engine, model, tier; metabot.ladder
    ladder_attempts: int = 3  # rungs one task may run; at most 5


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
    "fan_cap": (lambda v: isinstance(v, int) and not isinstance(v, bool), "a whole number"),
    "project_receipts": (lambda v: isinstance(v, bool), "true or false"),
    "prices": (_prices, "a table of model to its in and out dollars per million tokens"),
    "ladder_attempts": (
        lambda v: isinstance(v, int) and not isinstance(v, bool),
        "a whole number",
    ),
}


def _path():
    return metabot_home() / "config.json"


def load_config() -> Config:
    """The settings, or the defaults when there is no file; ConfigError when it does not read.

    A key MetaBot does not know is left alone, so a file a newer version wrote still reads.
    """
    path = _path()
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
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
