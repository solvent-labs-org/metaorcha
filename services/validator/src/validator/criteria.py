"""Declared acceptance criteria — digest composition on ``policy_version``.

RFC 0003's ``policy_version`` already names the ruleset. A turn may also
carry a criteria digest. Both facts share one signed field:

    ``<policy>+criteria:<64-hex>``

No new envelope field. Both halves are recoverable. A run with no declared
criteria keeps today's ``policy_version`` bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CRITERIA_MARKER = "+criteria:"


def canonical_criteria_bytes(criteria: dict[str, Any]) -> bytes:
    """Stable bytes for a criteria object (RFC 0003 canonical form)."""
    return json.dumps(criteria, sort_keys=True, separators=(",", ":")).encode("utf-8")


def criteria_digest(criteria: dict[str, Any]) -> str:
    """sha256 hex of the canonical criteria document."""
    return hashlib.sha256(canonical_criteria_bytes(criteria)).hexdigest()


def compose_policy_version(policy: str, digest: str | None) -> str:
    """Return ``policy`` or ``<policy>+criteria:<digest>``. Both halves recoverable."""
    if not digest:
        return policy
    if not _HEX64_RE.fullmatch(digest):
        raise ValueError("criteria digest must be 64 lowercase hex chars")
    return f"{policy}{CRITERIA_MARKER}{digest}"


def parse_policy_version(value: str) -> tuple[str, str | None]:
    """Split a composed ``policy_version`` into ``(policy, digest_or_none)``."""
    idx = value.find(CRITERIA_MARKER)
    if idx == -1:
        return value, None
    digest = value[idx + len(CRITERIA_MARKER) :]
    if not _HEX64_RE.fullmatch(digest):
        return value, None
    return value[:idx], digest
