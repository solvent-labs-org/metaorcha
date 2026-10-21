"""What a run spent: the tokens each engine reports, read one way.

Money is kept in whole micro-dollars, never a float, so a cost that is signed
reads the same everywhere it is checked.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

MUSD = 1_000_000  # micro-dollars in a dollar


@dataclass(frozen=True)
class Spend:
    input: int = 0  # input read at the full rate
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reported_musd: int | None = None  # what the engine said it cost, when it says

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: Spend) -> Spend:
        mine, theirs = self.reported_musd, other.reported_musd
        reported = None if mine is None and theirs is None else (mine or 0) + (theirs or 0)
        return Spend(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
            reported,
        )

    def as_dict(self) -> dict[str, int | None]:
        return asdict(self)


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def musd_of(value: Any) -> int | None:
    """A dollar figure an engine reported, in micro-dollars; None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return math.ceil(value * MUSD - 1e-6)  # never under what was said


def spend_of(data: Mapping[str, Any]) -> Spend:
    """One usage report, from any engine, as a Spend.

    ``codex`` and ``openai`` count cached input inside their input; here it is
    taken out, so each token is counted once and at its own rate.
    """
    if not isinstance(data, Mapping):
        return Spend()
    usage = data.get("usage", data)
    if not isinstance(usage, Mapping):
        return Spend()
    reported = musd_of(data.get("total_cost_usd"))
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        details = usage.get("prompt_tokens_details")
        cached = _count(details, "cached_tokens") if isinstance(details, Mapping) else 0
        prompt = _count(usage, "prompt_tokens")
        cached = min(cached, prompt)
        return Spend(prompt - cached, _count(usage, "completion_tokens"), cached)
    if "inputTokens" in usage or "outputTokens" in usage:
        return Spend(
            _count(usage, "inputTokens"),
            _count(usage, "outputTokens"),
            _count(usage, "cacheReadTokens"),
            _count(usage, "cacheWriteTokens"),
        )
    if "cached_input_tokens" in usage:
        whole = _count(usage, "input_tokens")
        cached = min(_count(usage, "cached_input_tokens"), whole)
        return Spend(
            whole - cached,
            _count(usage, "output_tokens"),
            cached,
            _count(usage, "cache_write_input_tokens"),
        )
    return Spend(
        _count(usage, "input_tokens"),
        _count(usage, "output_tokens"),
        _count(usage, "cache_read_input_tokens"),
        _count(usage, "cache_creation_input_tokens"),
        reported,
    )


def dollars(musd: int) -> str:
    """Micro-dollars as a dollar figure: cents when it is that much, else every digit."""
    whole = f"{musd / MUSD:.2f}"
    if musd == 0 or musd % 10_000 == 0:
        return f"${whole}"
    return f"${musd / MUSD:.6f}".rstrip("0")
