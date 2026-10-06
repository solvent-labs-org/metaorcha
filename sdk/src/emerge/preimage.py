"""The value a run step's ``output_hash`` commits to (RFC 0003, spine AD-16).

``output_hash = sha256_hex(canonical_json_bytes(pre-image))``, where the
pre-image is built from a tool call's **raw** result — before any display
formatting — by exactly two functions, applied in this order:

1. :func:`output_preimage` maps any handler result to a JSON value: strings,
   ints and booleans as-is; bytes to their sha256 hex; objects and arrays
   element-wise; a content block (an object carrying ``text``, ``data``,
   ``blob`` or ``resource``) to a string — its text, or the sha256 hex of its
   binary payload.
2. :func:`redact_credentials` replaces the exact bytes of every credential
   resolved for the call with the fixed marker ``[REDACTED:<VAR>]``.

Both are total (they never raise) and deterministic, so anyone holding the
redacted output re-derives the hash. Stdlib only: content blocks from any MCP
client library are read by attribute, never by importing the library.

This module is the first landing of the pre-image rule in the SDK; the
producer module ``emerge.record`` (draft) re-exports it rather than
redefining it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import enum
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

# Deeper nesting than this is cut with a fixed marker instead of recursing, so
# a pathological result can never exhaust the stack.
MAX_DEPTH = 64
DEPTH_MARKER = "[DEPTH]"
CYCLE_MARKER = "[CYCLE]"

_BLOCK_BINARY_ATTRS = ("data", "blob")


def redaction_marker(var: str) -> str:
    """The fixed text that replaces a credential named *var*."""
    return f"[REDACTED:{var}]"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_getattr(obj: Any, name: str) -> Any:
    """``getattr`` that treats a raising property as absent."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _binary_digest(payload: Any) -> str | None:
    """sha256 hex of a block's binary payload (raw bytes or base64 text)."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return _sha256_hex(bytes(payload))
    if isinstance(payload, str):
        try:
            return _sha256_hex(base64.b64decode(payload, validate=True))
        except (binascii.Error, ValueError):
            return _sha256_hex(payload.encode("utf-8", "surrogatepass"))
    return None


def _block_text(obj: Any, depth: int, active: set[int]) -> str | None:
    """The string a content block maps to, or None if *obj* is not a block."""
    text = _safe_getattr(obj, "text")
    if isinstance(text, str):
        return text
    for attr in _BLOCK_BINARY_ATTRS:
        digest = _binary_digest(_safe_getattr(obj, attr))
        if digest is not None:
            return digest
    resource = _safe_getattr(obj, "resource")
    if resource is not None and not isinstance(resource, (str, int, float)):
        if depth >= MAX_DEPTH:
            return DEPTH_MARKER
        return _block_text(resource, depth + 1, active)
    return None


def _key(key: Any) -> str:
    """A JSON object key for any mapping key."""
    if isinstance(key, str):
        return key
    if isinstance(key, (bytes, bytearray, memoryview)):
        return _sha256_hex(bytes(key))
    if key is None or isinstance(key, (bool, int, float)):
        return str(key)
    return type(key).__qualname__


def _overrides_str(obj: Any) -> bool:
    cls = type(obj)
    return cls.__str__ is not object.__str__ or cls.__repr__ is not object.__repr__


def _preimage(value: Any, depth: int, active: set[int]) -> Any:
    # Scalars are coerced to their exact built-in type, so a subclass (an
    # IntEnum, a str subclass with its own __str__) renders as its value.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else str(float(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _sha256_hex(bytes(value))
    if depth >= MAX_DEPTH:
        return DEPTH_MARKER
    marker = id(value)
    if marker in active:
        return CYCLE_MARKER
    active.add(marker)
    try:
        return _container_or_object(value, depth, active)
    finally:
        active.discard(marker)


def _container_or_object(value: Any, depth: int, active: set[int]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        try:
            items = list(value.items())
        except Exception:
            return type(value).__qualname__
        for key, item in items:
            out[_key(key)] = _preimage(item, depth + 1, active)
        return out
    if isinstance(value, (list, tuple)):
        return [_preimage(item, depth + 1, active) for item in value]
    if isinstance(value, (set, frozenset)):
        mapped = [_preimage(item, depth + 1, active) for item in value]
        return sorted(mapped, key=_sort_key)
    if isinstance(value, enum.Enum):
        return _preimage(value.value, depth + 1, active)

    block = _block_text(value, depth, active)
    if block is not None:
        return block

    # Each rendering below may raise on a hostile object; a failure falls
    # through to the next one.
    dump = _safe_getattr(value, "model_dump")
    if callable(dump):
        with contextlib.suppress(Exception):
            return _preimage(dump(mode="json"), depth + 1, active)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        with contextlib.suppress(Exception):
            return _preimage(dataclasses.asdict(value), depth + 1, active)
    if _overrides_str(value):
        with contextlib.suppress(Exception):
            return str(value)
    # No stable rendering exists (a default repr carries a memory address);
    # the type name is the deterministic floor.
    return type(value).__qualname__


def _sort_key(item: Any) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def output_preimage(result: Any) -> Any:
    """Map any handler result to the JSON value its ``output_hash`` covers.

    Never raises. The returned value contains only JSON types (``None``,
    ``bool``, ``int``, finite ``float``, ``str``, ``list``, ``dict`` with
    ``str`` keys).
    """
    try:
        return _preimage(result, 0, set())
    except Exception:
        return type(result).__qualname__


def _credential_pairs(
    credentials: Mapping[str, str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    raw = credentials.items() if isinstance(credentials, Mapping) else credentials
    # One marker per secret: when two names resolve to the same bytes, the
    # lexicographically smallest name wins, so the choice is deterministic.
    by_secret: dict[str, str] = {}
    for var, secret in raw:
        if not isinstance(var, str) or not isinstance(secret, str) or not secret:
            continue
        if secret not in by_secret or var < by_secret[secret]:
            by_secret[secret] = var
    return [(by_secret[s], s) for s in sorted(by_secret, key=lambda s: (-len(s), s))]


def _redact(value: Any, pattern: re.Pattern[str], names: dict[str, str]) -> Any:
    if isinstance(value, str):
        return pattern.sub(lambda m: redaction_marker(names[m.group(0)]), value)
    if isinstance(value, list):
        return [_redact(item, pattern, names) for item in value]
    if isinstance(value, dict):
        return {
            _redact(key, pattern, names): _redact(item, pattern, names)
            for key, item in value.items()
        }
    return value


def redact_credentials(
    preimage: Any,
    credentials: Mapping[str, str] | Iterable[tuple[str, str]],
) -> Any:
    """Replace every credential's exact bytes in *preimage* with its marker.

    *credentials* is ``{VAR: secret}`` or ``(VAR, secret)`` pairs; empty
    secrets are ignored. Matching is one left-to-right pass with longer
    secrets preferred, so a marker is never itself rewritten and a secret that
    contains a shorter one is replaced whole. Applies to every string in the
    value, object keys included. Never raises; on an unusable credential set
    the value is returned unchanged.
    """
    try:
        pairs = _credential_pairs(credentials)
        if not pairs:
            return preimage
        names = {secret: var for var, secret in pairs}
        pattern = re.compile("|".join(re.escape(secret) for _, secret in pairs))
        return _redact(preimage, pattern, names)
    except Exception:
        return preimage


__all__ = [
    "CYCLE_MARKER",
    "DEPTH_MARKER",
    "MAX_DEPTH",
    "output_preimage",
    "redact_credentials",
    "redaction_marker",
]
