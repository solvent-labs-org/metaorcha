"""Hostile bytes through the gate parsers stay inside their contracts."""

from __future__ import annotations

import random
import re
import traceback
from collections.abc import Mapping
from typing import Any

from metabot import origins, shell_policy
from metabot.scope_classes import ScopeClass

SEED = 20261013
ROUNDS = 64

_ORIGIN = re.compile(r"^https?://[^/]+:[0-9]+$")
_ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_PUNCT = ":/?#[]@!$&'()*+,;=%._-~\\"
_SPACE = " \t\r\n\x0b\x0c"
_CTRL = "\x00\x01\x1b\x7f"
_DIGITS = "²①٣١"
_PIECES = ("http://", "https://", "0x", "localhost", "127.0.0.1", "[::1]")
_SHELL = _ASCII + _PUNCT + _SPACE + _CTRL + _DIGITS + "|&;<>()$`{}'\"\n"
_CLEAN = (
    "http://localhost/",
    "https://127.0.0.1:8443/a",
    "http://[::1]/",
    "https://example.com",
)


def _rng() -> random.Random:
    return random.Random(SEED)


def _chunk(rng: random.Random) -> str:
    roll = rng.randrange(6)
    if roll == 0:
        return rng.choice(_PIECES)
    if roll == 1:
        return rng.choice(_DIGITS)
    if roll == 2:
        return rng.choice(_PUNCT + _SPACE + _CTRL)
    if roll == 3:
        return "".join(rng.choice(_ASCII) for _ in range(rng.randint(1, 6)))
    if roll == 4:
        return rng.choice(_DIGITS) + rng.choice(_ASCII)
    return rng.choice(_PIECES) + rng.choice(_DIGITS)


def _text(rng: random.Random) -> str:
    return "".join(_chunk(rng) for _ in range(rng.randint(1, 5)))


def _quad(rng: random.Random, n: int) -> str:
    """Four labels, the last a digit outside ASCII, which ``int()`` rejects."""
    last = _DIGITS[(n // 8) % len(_DIGITS)]
    labels = [str(rng.randint(0, 9)) for _ in range(3)]
    labels.append(last)
    scheme = "http://" if n % 16 == 0 else "https://"
    return scheme + ".".join(labels) + "/"


def _url(rng: random.Random, n: int) -> str:
    if n % 8 == 0:
        return _quad(rng, n)
    if n % 11 == 1:
        return rng.choice(_CLEAN)
    return _text(rng)


def _origin_input(rng: random.Random, n: int) -> object:
    if n % 8 == 0:
        return _quad(rng, n)
    if n % 11 == 1:
        return rng.choice(_CLEAN)
    roll = rng.randrange(6)
    if roll == 0:
        return None
    if roll == 1:
        return rng.randint(0, 9)
    if roll == 2:
        return rng.choice(_DIGITS)
    return _text(rng)


def _last_line(exc: BaseException) -> str:
    return traceback.format_exception_only(type(exc), exc)[-1].rstrip("\n")


def test_origin_of_returns_an_origin_or_none_and_never_raises() -> None:
    rng = _rng()
    for n in range(ROUNDS):
        url = _origin_input(rng, n)
        try:
            got = origins.origin_of(url)
        except Exception as exc:  # a parser bug must not escape
            raise AssertionError(f"round {n}: origin_of({url!r}) raised {_last_line(exc)}") from exc
        assert got is None or (isinstance(got, str) and _ORIGIN.fullmatch(got)), (
            f"round {n}: origin_of({url!r}) -> {got!r}"
        )


def test_origin_pattern_returns_text_or_value_error() -> None:
    rng = _rng()
    for n in range(ROUNDS):
        text = _url(rng, n)
        try:
            got = origins.origin_pattern(text)
        except ValueError:
            continue
        except Exception as exc:  # only ValueError is part of the contract
            raise AssertionError(
                f"round {n}: origin_pattern({text!r}) raised {_last_line(exc)}"
            ) from exc
        assert isinstance(got, str), f"round {n}: origin_pattern({text!r}) -> {got!r}"


def _leaf(rng: random.Random) -> object:
    roll = rng.randrange(5)
    if roll == 0:
        return _text(rng)
    if roll == 1:
        return None
    if roll == 2:
        return rng.choice((True, False))
    if roll == 3:
        return rng.choice(_DIGITS)
    return rng.randint(-1, 4)


def _tree(rng: random.Random, depth: int) -> object:
    if depth == 0 or rng.randrange(3) == 0:
        return _leaf(rng)
    if rng.randrange(2) == 0:
        return [_tree(rng, depth - 1) for _ in range(rng.randint(0, 2))]
    node: dict[str, object] = {}
    for i in range(rng.randint(0, 2)):
        key = rng.choice(("url", "urls", "uri", "href", "target", "targets", "page", "x"))
        node[f"{key}-{i}"] = _tree(rng, depth - 1)
    if rng.randrange(2) == 0:
        node["url"] = _leaf(rng)
    if rng.randrange(3) == 0:
        node["urls"] = [_leaf(rng), _text(rng)]
    return node


def _args(rng: random.Random, n: int) -> Mapping[str, Any] | None:
    if n % 5 == 0:
        return None
    built = _tree(rng, 3)
    if isinstance(built, dict):
        return built
    if isinstance(built, list):
        return {"urls": built}
    return {"url": built}


def test_call_urls_returns_a_list_and_never_raises() -> None:
    rng = _rng()
    for n in range(ROUNDS):
        args = _args(rng, n)
        try:
            got = origins.call_urls(args)
        except Exception as exc:  # a walk of model arguments must not escape
            raise AssertionError(f"round {n}: call_urls raised {_last_line(exc)}") from exc
        assert isinstance(got, list), f"round {n}: call_urls returned {type(got).__name__}"


def _cmd(rng: random.Random, n: int) -> str:
    if n % 8 == 0:
        return _quad(rng, n)
    if n % 8 == 1:
        return rng.choice(_DIGITS) + ">"
    return "".join(rng.choice(_SHELL) for _ in range(rng.randint(0, 16)))


def test_the_shell_parser_never_raises_on_hostile_lines() -> None:
    """``classify_shell`` turns any exception into the destructive class, so a
    parser bug there shows only as a refusal. The parser under it is held to
    never raise, and the public answer to match it."""
    rng = _rng()
    for n in range(ROUNDS):
        cmd = _cmd(rng, n)
        try:
            got = shell_policy._classify(cmd, 0, strict=True)
        except Exception as exc:  # classify_shell would hide this as a refusal
            raise AssertionError(f"round {n}: _classify({cmd!r}) raised {_last_line(exc)}") from exc
        assert isinstance(got, ScopeClass), f"round {n}: _classify({cmd!r}) -> {got!r}"
        assert shell_policy.classify_shell(cmd) is got, f"round {n}: classify_shell({cmd!r})"
