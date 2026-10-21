"""What a model's tokens cost, and where that figure comes from.

The table holds Anthropic's listed rates, read on ``PRICES_AS_OF``. The person
adds or overrides a rate under ``prices`` in ``config.json``, in dollars per
million tokens; their figure wins. A run's receipt names the table it was
priced by (``prices_digest``), so a cost can be checked against its rates.

Where a cost comes from:

- ``engine``: the engine reported it (``claude``); under a plan login it is
  what the API would have charged.
- ``table`` or ``config``: tokens times a rate.
- ``local``: a model on this machine; nothing is paid per token.
- ``plan``: ``codex`` and ``cursor`` bill their own login; MetaBot sees tokens, no price.
- ``unknown``: no rate for the model.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from metabot.spend import MUSD, Spend

PRICES_AS_OF = "2026-10-21"
PRICE_KEYS = ("in", "out", "cache_read", "cache_write")


@dataclass(frozen=True)
class Rate:
    """Micro-dollars per million tokens of each kind."""

    input: int
    output: int
    cache_write: int
    cache_read: int
    above: tuple[int, Rate] | None = None  # past this many prompt tokens in one report

    def cost(self, spend: Spend) -> int:
        """What ``spend`` costs in micro-dollars, rounded up."""
        prompt = spend.input + spend.cache_read + spend.cache_write
        rate = self.above[1] if self.above and prompt > self.above[0] else self
        total = (
            spend.input * rate.input
            + spend.output * rate.output
            + spend.cache_write * rate.cache_write
            + spend.cache_read * rate.cache_read
        )
        return -(-total // MUSD)


def _musd(dollars: str) -> int:
    return int(Decimal(dollars) * MUSD)


def _rate(inp: str, out: str, read: str | None = None) -> Rate:
    """A listed rate: a cache write is 1.25x input; a read is 0.1x unless listed."""
    return Rate(
        input=_musd(inp),
        output=_musd(out),
        cache_write=_musd(inp) * 5 // 4,
        cache_read=_musd(read) if read else _musd(inp) // 10,
    )


_OPUS_5 = _rate("5", "25")
_FABLE_5_1 = _rate("10", "50", read="0.25")
ANTHROPIC: dict[str, Rate] = {
    "claude-fable-5-1": _FABLE_5_1,
    "claude-mythos-5-1": _FABLE_5_1,
    "claude-fable-5": _rate("10", "50", read="1"),
    "claude-mythos-5": _rate("10", "50", read="1"),
    "claude-opus-5-5": _rate("4", "20", read="0.20"),
    "claude-opus-5": _OPUS_5,
    "claude-opus-4-8": _OPUS_5,
    "claude-opus-4-7": _OPUS_5,
    "claude-opus-4-6": _OPUS_5,
    "claude-sonnet-5-5": _rate("2", "10", read="0.20"),
    "claude-sonnet-5": _rate("2", "10", read="0.20"),
    "claude-sonnet-4-6": _rate("3", "15"),
    "claude-haiku-5-5": Rate(
        **{k: v for k, v in vars(_rate("0.10", "0.50")).items() if k != "above"},
        above=(100_000, _rate("0.50", "2.50")),
    ),
    "claude-haiku-4-5": _rate("1", "5"),
}


@dataclass(frozen=True)
class Price:
    source: str  # engine | table | config | local | plan | unknown
    rate: Rate | None = None


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def read_prices(value: Any) -> dict[str, Rate]:
    """The person's ``prices`` setting as rates; ValueError when it does not read.

    ``{"model": {"in": 0.15, "out": 0.6}}`` in dollars per million tokens;
    ``cache_read`` and ``cache_write`` default to the input rate.
    """
    if not isinstance(value, Mapping):
        raise ValueError("prices is not a table of model to rates")
    out: dict[str, Rate] = {}
    for model, rates in value.items():
        if not isinstance(model, str) or not model:
            raise ValueError("prices names a model with no name")
        if not isinstance(rates, Mapping) or not {"in", "out"} <= set(rates):
            raise ValueError(f"prices for {model} needs in and out")
        if set(rates) - set(PRICE_KEYS) or not all(_number(v) for v in rates.values()):
            raise ValueError(f"prices for {model}: each of {', '.join(PRICE_KEYS)} is dollars")
        musd = {k: math.ceil(Decimal(str(v)) * MUSD) for k, v in rates.items()}
        out[model] = Rate(
            input=musd["in"],
            output=musd["out"],
            cache_write=musd.get("cache_write", musd["in"]),
            cache_read=musd.get("cache_read", musd["in"]),
        )
    return out


_SUFFIX = re.compile(r"\[[^\]]*\]$")  # claude's "[1m]"
_DATED = re.compile(r"-\d{8}")  # a snapshot of the model: claude-haiku-4-5-20251001


def _claude_name(model: str) -> str:
    name = _SUFFIX.sub("", model)
    at = name.find("claude-")
    return name[at:] if at > 0 and name[at - 1] in "./" else name


def _match(model: str, rates: Mapping[str, Rate]) -> str | None:
    if model in rates:
        return model
    name = _claude_name(model)
    for key in sorted(rates, key=len, reverse=True):
        if name == key or _DATED.fullmatch(name[len(key) :]) and name.startswith(key):
            return key
    return None


def rate_for(model: str | None, mine: Mapping[str, Rate]) -> Rate | None:
    """The rate for ``model``: the person's, else the table's; None when neither has it."""
    if not model:
        return None
    key = _match(model, {**ANTHROPIC, **mine})
    return None if key is None else {**ANTHROPIC, **mine}[key]


def is_local(base_url: str | None) -> bool:
    host = urlsplit(base_url or "").hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def price_for(
    engine: str, model: str | None, base_url: str | None, mine: Mapping[str, Rate]
) -> Price:
    """How a run on ``engine`` and ``model`` is priced, and by what."""
    if model and _match(model, mine):
        return Price("config", rate_for(model, mine))
    if engine == "claude":
        return Price("engine", rate_for(model, mine))
    if engine == "openai" and is_local(base_url):
        return Price("local", Rate(0, 0, 0, 0))
    if engine in ("codex", "cursor"):
        return Price("plan")
    rate = rate_for(model, mine)
    return Price("table", rate) if rate else Price("unknown")


def _rate_json(rate: Rate) -> list[Any]:
    tier = [rate.above[0], _rate_json(rate.above[1])] if rate.above else None
    return [rate.input, rate.output, rate.cache_write, rate.cache_read, tier]


def prices_digest(mine: Mapping[str, Rate]) -> str:
    """Sixteen hex digits that name the rates a run was priced by."""
    rates = {k: _rate_json(v) for k, v in {**ANTHROPIC, **mine}.items()}
    text = json.dumps([PRICES_AS_OF, rates], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
