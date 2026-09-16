"""Evaluate a turn's declared acceptance criteria (machine-checkable).

v1 understands ``citations_required``. The digest of the criteria document
is composed into ``policy_version`` by the run observer — this module only
evaluates and hashes.

Criteria are evaluated **per step** against that step's content. A multi-tool
turn that declares ``citations_required`` therefore fails on the first
non-citing step. Single-agent turns are unaffected.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SUPPORTED_CRITERIA = frozenset({"citations_required"})

_CITATION_REQUIRED_FIELDS = ("chunk_id", "source_title", "excerpt")


def has_valid_citations(content: str) -> bool:
    """True when *content* is JSON with a non-empty, well-formed citations list."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    citations = payload.get("citations")
    if not isinstance(citations, list) or not citations:
        return False
    return all(
        isinstance(c, dict) and all(c.get(field) for field in _CITATION_REQUIRED_FIELDS)
        for c in citations
    )


# Keep this byte-identical to validator.criteria.canonical_criteria_bytes so
# the digest in the signed envelope matches what the pipeline recorded.
def criteria_digest(criteria: dict[str, Any]) -> str:
    raw = json.dumps(criteria, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def evaluate_declared_criteria(
    criteria: dict[str, Any], content: str
) -> tuple[bool, str]:
    """Return (accepted, reason) for the declared document against *content*.

    Unknown keys fail closed so they cannot be signed as
    ``declared_acceptance: pass``. Evaluation is per step: a multi-tool turn
    with ``citations_required`` fails on the first non-citing step.
    """
    for key in criteria:
        if key not in SUPPORTED_CRITERIA:
            return False, f"unsupported criterion: {key}"
    if criteria.get("citations_required") and not has_valid_citations(content):
        return False, "missing citations"
    return True, "ok"
