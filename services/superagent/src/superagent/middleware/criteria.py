"""Evaluate a turn's declared acceptance criteria (machine-checkable).

v1 understands ``citations_required``. The digest of the criteria document
is composed into ``policy_version`` by the run observer — this module only
evaluates and hashes.

Criteria are evaluated **per step** against that step's content. A multi-tool
turn that declares ``citations_required`` therefore fails on the first
non-citing step. Single-agent turns are unaffected.

Run-level criteria (``RUN_CRITERIA``, story 2.4) are not evaluated here. They
compare steps with one another, so the pipeline stamps their operands on every
step (``run_criteria_meta``) and the run observer evaluates them at seal. They
are a routine's vocabulary only: chat has no operand carrier, so
``SUPPORTED_CRITERIA`` (the chat path's rule) does not include them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SUPPORTED_CRITERIA = frozenset({"citations_required"})
RUN_CRITERIA = frozenset({"counts_match"})
ROUTINE_CRITERIA = SUPPORTED_CRITERIA | RUN_CRITERIA
COUNTS_MATCH_OPERANDS = frozenset({"left", "right", "left_path", "right_path", "key"})

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


def step_criteria(criteria: dict[str, Any]) -> dict[str, Any]:
    """The criteria evaluated per step: everything but the run-level ones."""
    return {k: v for k, v in criteria.items() if k not in RUN_CRITERIA}


def run_criteria_meta(criteria: dict[str, Any], routine_context: Any) -> dict[str, Any]:
    """The run-level criteria a step carries for the observer: ``{name: operands}``.

    Only criteria declared ``true`` are carried. Operands come from the
    routine; a missing or malformed operand document is carried as ``{}`` so
    the observer signs ``no operands declared`` rather than skipping the check.
    """
    if criteria.get("counts_match") is not True:
        return {}
    operands: Any = {}
    if isinstance(routine_context, dict):
        operands = routine_context.get("criteria_operands")
    spec = operands.get("counts_match") if isinstance(operands, dict) else None
    return {"counts_match": dict(spec) if isinstance(spec, dict) else {}}


def criteria_step_meta(
    criteria: dict[str, Any], routine_context: Any
) -> dict[str, Any]:
    """The step metadata the run observer reads: the digest, and any run criteria.

    The one writer of these keys (``validator.run_observer`` reads them); the
    validator's contract test builds its steps through this function.
    """
    meta: dict[str, Any] = {"criteria_digest": criteria_digest(criteria)}
    run = run_criteria_meta(criteria, routine_context)
    if run:
        meta["run_criteria"] = run
    return meta
