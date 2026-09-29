"""Evaluate a turn's declared acceptance criteria (machine-checkable).

v1 understands ``citations_required`` and ``exit_zero``. The digest of the
criteria document is composed into ``policy_version`` by the run observer —
this module only evaluates and hashes.

Criteria are evaluated **per step** against that step's content, and each
criterion has three outcomes on a step: ``pass``, ``fail``, or ``n/a`` (the
step carries nothing the criterion can read). ``citations_required`` applies
to every step. ``exit_zero`` applies only to a step whose output carries an
exit code — a read or a patch step is ``n/a``, not a failure.

The run-level rule lives in ``validator.run_observer._declared_acceptance``:
per criterion, ``fail`` if any applicable step failed, ``pass`` if at least
one applicable step passed and none failed, and a run in which **no** step was
applicable to a declared criterion fails ("no step reported an exit code") —
declaring ``exit_zero`` and never running anything is not acceptance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SUPPORTED_CRITERIA = frozenset({"citations_required", "exit_zero"})
_EXIT_KEYS = ("exit_code", "returncode", "exit")

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


def parse_exit_code(content: str) -> int | None:
    """Read an integer exit code from JSON ``exit_code`` / ``returncode`` / ``exit``.

    Bool is not an exit code (``True`` is a subclass of ``int``). Missing or
    unparseable content returns ``None`` so ``exit_zero`` can fail closed.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in _EXIT_KEYS:
        raw = payload.get(key)
        if type(raw) is int:
            return raw
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
    return None


NOT_APPLICABLE = "n/a"


def evaluate_criteria(
    criteria: dict[str, Any], content: str
) -> dict[str, dict[str, str]]:
    """Per-criterion result for one step: ``{key: {"result", "detail"}}``.

    ``result`` is ``pass`` | ``fail`` | ``n/a``. Unknown keys are ``fail``
    (closed) so they can never be signed as accepted. A key declared ``false``
    is not a declaration and is omitted.
    """
    results: dict[str, dict[str, str]] = {}
    for key in criteria:
        if key not in SUPPORTED_CRITERIA:
            results[key] = {"result": "fail", "detail": f"unsupported criterion: {key}"}
            continue
        if not criteria.get(key):
            continue
        if key == "citations_required":
            ok = has_valid_citations(content)
            results[key] = {
                "result": "pass" if ok else "fail",
                "detail": "ok" if ok else "missing citations",
            }
        elif key == "exit_zero":
            code = parse_exit_code(content)
            if code is None:
                results[key] = {
                    "result": NOT_APPLICABLE,
                    "detail": "no exit code in step output",
                }
            elif code != 0:
                results[key] = {"result": "fail", "detail": f"nonzero exit: {code}"}
            else:
                results[key] = {"result": "pass", "detail": "ok"}
    return results


def step_declared_acceptance(criteria: dict[str, Any], content: str) -> dict[str, Any]:
    """The ``declared_acceptance`` step-metadata entry.

    ``{"result": pass|fail|n/a, "detail": str, "criteria": {key: result}}`` —
    ``fail`` if any criterion failed on this step, ``n/a`` if no declared
    criterion applies to it, else ``pass``. The per-criterion map is what the
    run observer aggregates; ``result`` is the step's own summary.
    """
    per = evaluate_criteria(criteria, content)
    failed = [v for v in per.values() if v["result"] == "fail"]
    applicable = [v for v in per.values() if v["result"] != NOT_APPLICABLE]
    if failed:
        result, detail = "fail", failed[0]["detail"]
    elif per and not applicable:
        result, detail = NOT_APPLICABLE, next(iter(per.values()))["detail"]
    else:
        # nothing declared, or every declared criterion applied and passed
        result, detail = "pass", "ok"
    return {
        "result": result,
        "detail": detail,
        "criteria": {k: v["result"] for k, v in per.items()},
    }


def evaluate_declared_criteria(
    criteria: dict[str, Any], content: str
) -> tuple[bool, str]:
    """Step-level ``(accepted, reason)``: accepted unless a criterion **failed**.

    A step on which a criterion is ``n/a`` is not a failure of that step; the
    run-level rule decides whether the run as a whole satisfied it.
    """
    entry = step_declared_acceptance(criteria, content)
    return entry["result"] != "fail", str(entry["detail"])
