"""Where a browser connection may act, and the class of each call by the origin it acts on.

A browser connection's server drives a web browser. Each of its calls is
classed by the origin it acts on: the origin of every url the call carries,
at any depth and in a list under a plural name such as ``urls`` or
``targets``, else the origin the page was last sent to. On an allowed origin a call is read when
its name says it only looks, and write otherwise; a destructive word in its
name stays destructive. A call that runs code in the page is destructive on
every origin: its code can act anywhere, and send what it reads anywhere. So
is a call on the browser's cookies, storage or routes, which reach every
site, and a call at a point on the screen, which may land in another site's
frame. On any other origin, or when the origin is not known, the call is
destructive, so a person approves that exact call, and a run with nobody
there refuses it. The person's own dev server, on this machine and any port,
is allowed unless they say otherwise.

A target names an element of the page only as a browser server names one
of the top page, ``e6``; anything else under a key such as ``target`` or
``endTarget`` counts as a place. An element in a frame, ``f1e12``, is in a
page that may be another site's, so it is a place with no origin.

An origin is read only from an absolute http or https url that every
browser reads the same way. A url a browser could read differently, such as
one with a backslash, a space or control character, a percent sign or a
letter outside ASCII in its host, has no origin, so a call that carries it
is destructive. A host spelled as a decimal, hex or octal number, or an
IPv4 address inside an IPv6 one, has no origin either: a browser may read
it as another address. So does a host whose last label is a number, unless
the host is four decimal octets. One trailing dot on a name is that name
without the dot.

The page's origin is learnt from the calls that carry a url, and from a
server that says where its page is after a call, in a line
``- Page URL: <url>`` of its answer, as a browser MCP server does. Lines that
disagree leave the origin unknown. A page can write such a line itself, in its
title or a console message, so a line may take the page off an allowed origin
and never put it on one: only a call that carries the url brings it back. A
call that moves the page without saying where, back, forward, to another tab,
or to another page, leaves its origin unknown. A call that reads the page, or
acts on it where it is, keeps that origin. With a server that does not say, a
page that moves itself, by a link or a redirect, is not seen: the next call is
classed by where the page was last sent, and the person is shown that when
asked.
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from metabot.scope_classes import (
    ScopeClass,
    _tokens,
    is_recognised,
    is_valid_capability_id,
    platform_rule,
)
from metabot.text import ascii_digits

DEFAULT_ORIGINS = ("http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*")
ANY_PORT = "*"
SCHEMES = {"http": 80, "https": 443}

# A browser reads these differently from Python, or not at all the same way.
_UNSAFE = re.compile(r"[\\\s\x00-\x1f\x7f]")
_HOST = re.compile(r"^[a-z0-9_.-]+$")
_URL_KEYS = frozenset({"url", "uri", "href", "link", "location", "address", "target"})
_URL_LISTS = frozenset({"urls", "uris", "hrefs", "links", "locations", "addresses", "targets"})
# Keys a browser server also uses to name an element of the page: target, and any key that
# ends so, such as endTarget. An element of the top page is named e<N>; one in a frame,
# f<N>e<M>, is in a page that may be another site's, so it counts as a place.
_ELEMENT_OR_PLACE = ("target", "targets")
_ELEMENT_REF = re.compile(r"e[0-9]{1,9}")

# Words of a call that runs code in the page or the browser.
RUNS_CODE = frozenset({"evaluate", "eval", "code", "script", "javascript", "js", "execute", "exec"})
# Words of a call on the whole browser: its cookies, storage and routes reach every site.
REACHES_EVERY_SITE = frozenset({"cookie", "cookies", "storage", "route", "routes", "unroute"})
# Words of a call at a point on the screen: what is there may be another site's frame.
AT_A_POINT = frozenset({"mouse", "xy"})
# Words of a call that moves the page somewhere it does not say.
MOVES_UNSAID = frozenset({"back", "forward", "tab", "tabs", "history"})
# With the word "page", these change which page is showing.
_PAGE_CHANGE = frozenset({"select", "activate", "switch", "new", "close"})

# Words that only look at a page, beside the platform's read words.
BROWSER_READS = frozenset({"screenshot", "snapshot"})
# Words that act on a page even when a read word stands beside them.
BROWSER_ACTIONS = frozenset(
    {
        "click",
        "type",
        "press",
        "key",
        "fill",
        "drag",
        "hover",
        "tap",
        "scroll",
        "select",
        "check",
        "uncheck",
        "navigate",
        "goto",
        "download",
        "upload",
        "submit",
    }
)


def _ipv4_part(part: str) -> int | None:
    """One IPv4 label as a browser reads it, or None when the label is not one."""
    if part.lower().startswith("0x"):
        rest = part[2:]
        if any(c not in "0123456789abcdef" for c in rest.lower()):
            return None
        return int(rest, 16) if rest else 0
    if not ascii_digits(part):
        return None
    if len(part) > 1 and part[0] == "0":
        if any(c in "89" for c in part):
            return None
        return int(part, 8)
    return int(part, 10)


def _ipv4_spelled_another_way(host: str) -> bool:
    """Whether this host is an IPv4 address in any spelling but the shared one.

    Four decimal octets, each 0 to 255 with no leading zero, are the spelling
    browsers share. Anything else that is only numbers (a shorter form, a
    longer one, hex, octal, an empty hex label, or an octet past 255) is not
    an origin. A browser may read some of those as another address and reject
    the rest.
    """
    parts = host.split(".")
    numbers = [n for n in map(_ipv4_part, parts) if n is not None]
    if len(numbers) != len(parts):
        return False
    if len(parts) != 4:
        return True
    return not all(
        ascii_digits(part) and (part == "0" or part[0] != "0") and number <= 255
        for part, number in zip(parts, numbers, strict=True)
    )


def _label_is_a_number(label: str) -> bool:
    """Whether browsers try this label as an IPv4 piece: digits, or ``0x`` and hex digits."""
    if ascii_digits(label):
        return True
    if not label.lower().startswith("0x"):
        return False
    return all(c in "0123456789abcdef" for c in label[2:].lower())


def _four_plain_octets(host: str) -> bool:
    """The IPv4 spelling browsers share: four decimal octets, 0 to 255, no leading zero."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not ascii_digits(part) or (len(part) > 1 and part[0] == "0"):
            return False
        if int(part, 10) > 255:
            return False
    return True


def _ends_in_a_number(host: str) -> bool:
    """Whether browsers read this host as IPv4, which only four plain decimal octets spell."""
    return _label_is_a_number(host.rsplit(".", 1)[-1]) and not _four_plain_octets(host)


def _host(raw: str | None) -> str | None:
    """A url's host as every browser reads it, or None when one could read it otherwise."""
    if not raw or "%" in raw:
        return None
    if ":" in raw:  # an IPv6 address; urlsplit has taken off its brackets
        try:
            addr = ipaddress.IPv6Address(raw)
        except ValueError:
            return None
        if addr.ipv4_mapped is not None:
            return None  # a browser may read this as the IPv4 address
        return f"[{addr.compressed}]"
    host = raw.lower()
    if host.endswith(".") and not host.endswith(".."):
        host = host[:-1]
    if (
        not host
        or ".." in host
        or host.startswith(".")
        or host.endswith(".")
        or not _HOST.match(host)
        or _ipv4_spelled_another_way(host)
        or _ends_in_a_number(host)
    ):
        return None
    return host


def origin_of(url: Any) -> str | None:
    """``scheme://host:port`` of an absolute http(s) url; None for anything else.

    ``file:``, ``data:``, ``javascript:`` and ``about:`` urls have no origin,
    and neither has a relative url or one a browser could read differently.
    """
    if not isinstance(url, str) or _UNSAFE.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in SCHEMES:
        return None
    host = _host(parts.hostname)
    if host is None:
        return None
    return f"{parts.scheme}://{host}:{SCHEMES[parts.scheme] if port is None else port}"


def origin_pattern(text: str) -> str:
    """An origin the person allows, made exact: ``scheme://host:port``, or ``:*`` for any port.

    ValueError when it is not one: a path, a query, a name before the host, a
    wildcard in the host, or a host outside ASCII, which is asked for in its
    ``xn--`` form, the one a browser sends.
    """
    raw = text.strip()
    bare = raw.removesuffix("/")
    wild = bare.endswith(":" + ANY_PORT)
    probe = bare.removesuffix(":" + ANY_PORT) if wild else raw
    shape = "write it as http(s)://host, with :port, or :* for any port"
    bad = ValueError(f"{text!r} is not an origin; {shape}")
    if not raw or _UNSAFE.search(raw):
        raise bad
    try:
        parts = urlsplit(probe)
        port = parts.port
    except ValueError:
        raise bad from None
    if (
        parts.scheme not in SCHEMES
        or "@" in parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or (wild and port is not None)
    ):
        raise bad
    if parts.hostname and not parts.hostname.isascii():
        raise ValueError(f"{text!r}: write a host outside ASCII in its xn-- form")
    host = _host(parts.hostname)
    if host is None:
        raise bad
    if wild:
        return f"{parts.scheme}://{host}:{ANY_PORT}"
    return f"{parts.scheme}://{host}:{SCHEMES[parts.scheme] if port is None else port}"


def origins_for(given: Iterable[str]) -> list[str]:
    """This machine's origins, then the given ones made exact, each once; ValueError if bad."""
    out: list[str] = []
    for pattern in [*DEFAULT_ORIGINS, *(origin_pattern(g) for g in given)]:
        if pattern not in out:
            out.append(pattern)
    return out


def origin_allowed(origin: str | None, patterns: Iterable[str]) -> bool:
    """Whether an exact origin matches one of the patterns; an unknown origin never does."""
    if origin is None:
        return False
    site, _, port = origin.rpartition(":")
    for pattern in patterns:
        allowed_site, _, allowed_port = pattern.rpartition(":")
        if allowed_site == site and allowed_port in (ANY_PORT, port):
            return True
    return False


def _is_url_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    low = key.lower()
    return low in _URL_KEYS or low.endswith(("url", "uri", "target"))


def _is_url_list_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    low = key.lower()
    return low in _URL_LISTS or low.endswith(("urls", "uris", "targets"))


def call_urls(args: Mapping[str, Any] | None) -> list[Any]:
    """What a call's arguments name as a place to go, at any depth: url, uri, href, and the like.

    A list under a plural name, such as ``urls`` or ``targets``, holds one each. Any other
    value under such a name counts as one, so one that is not text has no
    origin, except an element of the page named under ``target``. The
    arguments are walked without recursion: their depth is the model's to
    choose.
    """
    found: list[Any] = []
    todo: deque[Any] = deque([args or {}])
    while todo:
        node = todo.popleft()
        if isinstance(node, list):
            todo.extend(node)
            continue
        if not isinstance(node, Mapping):
            continue
        for key, value in node.items():
            if _is_url_key(key):
                found.extend(_places(key, [value]))
            elif _is_url_list_key(key):
                found.extend(_places(key, value if isinstance(value, list) else [value]))
            elif isinstance(value, (Mapping, list)):
                todo.append(value)
    return found


def _places(key: str, values: list[Any]) -> list[Any]:
    """The values under a url key that may be a place: under a target, not an element's name.

    A browser server names an element of the top page as its target, such as
    ``e6``. Anything else still counts, an element in a frame among them.
    """
    if not key.lower().endswith(_ELEMENT_OR_PLACE):
        return values
    return [v for v in values if not (isinstance(v, str) and _ELEMENT_REF.fullmatch(v))]


_PAGE_URL = re.compile(r"- Page URL: (\S+)")


def page_said(texts: Iterable[str]) -> tuple[bool, str | None]:
    """Where a browser server says its page is after a call: whether it said, and the origin.

    A ``- Page URL: <url>`` line at the start of a line of its answer says it.
    Lines that disagree, or a url with no origin, say the page is not known.
    """
    found = {m.group(1) for text in texts for m in map(_PAGE_URL.fullmatch, text.splitlines()) if m}
    if not found:
        return False, None
    origins = {origin_of(url) for url in found}
    return True, origins.pop() if len(origins) == 1 else None


def acts_on_the_page(name: str) -> bool:
    """Whether a browser call's name says it acts on the page: a click, typing, a navigate."""
    return bool(set(_tokens(name)) & BROWSER_ACTIONS)


def moves_unsaid(name: str) -> bool:
    """Whether a call moves its page somewhere it does not say.

    That is back, forward, another tab, or another page.
    """
    words = set(_tokens(name))
    return bool(words & MOVES_UNSAID) or ("page" in words and bool(words & _PAGE_CHANGE))


def acts_on(args: Mapping[str, Any] | None, last: str | None) -> list[str | None]:
    """The origins a call acts on: those of its urls, else where its page was last sent."""
    urls = call_urls(args)
    return [origin_of(u) for u in urls] if urls else [last]


def browser_class(name: str, allowed: bool) -> ScopeClass:
    """A browser call's class before hints and settings tighten it."""
    if not allowed or not is_valid_capability_id(name):
        return ScopeClass.DESTRUCTIVE
    if set(_tokens(name)) & (RUNS_CODE | REACHES_EVERY_SITE | AT_A_POINT):
        return ScopeClass.DESTRUCTIVE  # it can act on any origin, whatever the page's
    named = platform_rule(name)
    if named is ScopeClass.DESTRUCTIVE and is_recognised(name):
        return ScopeClass.DESTRUCTIVE  # a destructive word in the name
    words = set(_tokens(name))
    if named is ScopeClass.WRITE or words & BROWSER_ACTIONS:
        return ScopeClass.WRITE
    if named is ScopeClass.READ or words & BROWSER_READS:
        return ScopeClass.READ
    return ScopeClass.WRITE  # an action the name does not class, on an allowed origin
