"""Whether a local model can call MetaBot's tools, and how: the probe, and what it found.

A probe is a real round trip through the ``openai`` engine itself, in a
read-only job in a temporary folder: the model is asked to read a file and
reply with the word in it. It passes when the model read the file with a
tool and answered with the word. It tries the model's own tool calls first,
which MetaBot repairs when the model writes a call as text, and then
constrained output. What it finds is kept per server and model in
``models.json``, and the engine reads it to pick how it asks.

Only a server on this machine is probed: a probe of a hosted model costs money.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.config import Config
from metabot.engines.base import EngineJob
from metabot.events import ErrorEvent, Final, Notice, ToolCall
from metabot.paths import metabot_home, models_path, write_private
from metabot.prices import is_local

MODES = ("native", "repaired", "json", "none")
REPAIRS = ("openai: read ", "openai: the model asked for ")  # the engine's notices of a repair
PROMPT = (
    "Read the file probe.txt in this directory with your tools. "
    "Then reply with the word it holds, and nothing else."
)


@dataclass(frozen=True)
class Probe:
    mode: str  # native | repaired | json | none | unchecked
    detail: str


@dataclass
class _Attempt:
    read: bool = False
    said: bool = False
    repaired: bool = False
    errors: list[str] = field(default_factory=list)


def _key(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')} {model}"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(models_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_probe(base_url: str, model: str, mode: str, detail: str) -> None:
    data = _read()
    data[_key(base_url, model)] = {"mode": mode, "detail": detail, "checked_at": utc_now()}
    metabot_home()  # made private, if this is its first file
    write_private(models_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def cached_mode(base_url: str, model: str) -> str | None:
    """What the last probe of this model found; None when it was never probed."""
    entry = _read().get(_key(base_url, model))
    mode = entry.get("mode") if isinstance(entry, dict) else None
    return mode if mode in MODES else None


def _word() -> str:
    return f"probe-{secrets.token_hex(3)}"


async def _deny(_msg: dict[str, Any]) -> dict[str, Any]:
    return {"allow": False, "reason": "a probe changes nothing"}


async def _attempt(
    base_url: str, model: str, key_env: str | None, mode: str, folder: Path, word: str
) -> _Attempt:
    from metabot.engines.openai_compat import OpenAIEngine

    engine = OpenAIEngine(
        base_url=base_url, model=model, key_env=key_env, config=Config(), tool_mode=mode,
        save_sessions=False,
    )  # fmt: skip
    job = EngineJob(prompt=PROMPT, cwd=folder, model=model, read_only=True)
    seen = _Attempt()
    async with contextlib.aclosing(engine.run(job, approve=_deny)) as events:
        async for event in events:
            if isinstance(event, ToolCall) and event.tool == "read_file":
                seen.read = seen.read or Path(str(event.args.get("path", ""))).name == "probe.txt"
            elif isinstance(event, Notice) and event.text.startswith(REPAIRS):
                seen.repaired = True
            elif isinstance(event, ErrorEvent):
                seen.errors.append(event.message)
            elif isinstance(event, Final):
                seen.said = word.lower() in event.text.lower()
    return seen


async def probe_model(
    base_url: str, model: str, *, key_env: str | None = None, word: str | None = None
) -> Probe:
    """How ``model`` on ``base_url`` can call MetaBot's tools, if it can."""
    if not is_local(base_url):
        return Probe("unchecked", "remote; not checked: a probe there costs money")
    word = word or _word()
    with tempfile.TemporaryDirectory(prefix="mb-probe-") as tmp:
        folder = Path(tmp)
        (folder / "probe.txt").write_text(word + "\n", encoding="utf-8")
        native = await _attempt(base_url, model, key_env, "native", folder, word)
        if native.read and native.said:
            if native.repaired:
                return Probe("repaired", "wrote its call as text, and MetaBot read it")
            return Probe("native", "called read_file and answered with the word")
        constrained = await _attempt(base_url, model, key_env, "json", folder, word)
        if constrained.read and constrained.said:
            return Probe("json", "needs constrained output: read the file and answered by schema")
    unreachable = [e for e in native.errors + constrained.errors if "answered" in e or "reach" in e]
    if unreachable:
        return Probe("none", unreachable[-1].removeprefix("openai: ").strip()[:200])
    if native.read or constrained.read:
        return Probe("none", "read the file but did not answer with its word")
    return Probe("none", "made no read of the file, as tools or by schema")
