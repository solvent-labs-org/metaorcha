"""Evaluate a turn's declared acceptance criteria (machine-checkable).

v1 understands ``citations_required`` and ``exit_zero``. The digest of the
criteria document is composed into ``policy_version`` by the run observer —
this module only evaluates and hashes.

Criteria are evaluated **per step** against that step's own output — the
agent's bytes, never the 280-character display card the normalizer builds
(FR-7, AD-16). An output made of content blocks (an MCP result with several
blocks, or the raw-HTTP fallback's ``{"content": [...]}`` wrapper) is read one
block at a time (``criteria_units``): a nonzero exit code in any block fails
``exit_zero``, and citations in any block satisfy ``citations_required``.
Each criterion has three outcomes on a step: ``pass``, ``fail``, or ``n/a``
(the step carries nothing the criterion can read). ``citations_required``
applies to every agent step and is ``n/a`` on a platform system tool's step
(``system_step_declared_acceptance``); ``exit_zero`` applies only to a step
whose output carries an exit code — a read or a patch step is ``n/a``, not a
failure.

Evaluation is total. The output is the agent's to choose, so a value that
breaks a parser (a non-numeric "exit code", JSON nested past the recursion
limit) must never raise out of the pipeline after the call was dispatched —
that would drop the step from the receipt on the agent's say-so. Such a
criterion is signed ``fail`` with ``CRITERIA_UNEVALUABLE``, and the step is
emitted.

The run-level rule lives in ``validator.run_observer._declared_acceptance``:
per criterion, ``fail`` if any applicable step failed, ``pass`` if at least
one applicable step passed and none failed, and a run in which **no** step was
applicable to a declared criterion fails ("no step reported an exit code") —
declaring ``exit_zero`` and never running anything is not acceptance.

Run-level criteria (``RUN_CRITERIA``, story 2.4) are not evaluated here. They
compare steps with one another, so the pipeline stamps their operands on every
step (``run_criteria_meta``) and the run observer evaluates them at seal. They
are a routine's vocabulary only: chat has no operand carrier, so
``SUPPORTED_CRITERIA`` (the chat path's rule) does not include them.
``exit_zero`` is chat-only for now: it and ``citations_required`` sign the one
``declared_acceptance`` verdict, and the routines pane names a criterion as
checked from that verdict, so a routine may not declare both until the
verdict says which criterion it judged.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SUPPORTED_CRITERIA = frozenset({"citations_required", "exit_zero"})
RUN_CRITERIA = frozenset({"counts_match"})
ROUTINE_CRITERIA = frozenset({"citations_required"}) | RUN_CRITERIA
COUNTS_MATCH_OPERANDS = frozenset({"left", "right", "left_path", "right_path", "key"})

_CITATION_REQUIRED_FIELDS = ("chunk_id", "source_title", "excerpt")
_EXIT_KEYS = ("exit_code", "returncode", "exit")

NOT_APPLICABLE = "n/a"
# The run observer signs this when exit_zero was declared and no step carried
# an exit code (validator.run_observer reads the same words).
NO_EXIT_CODE = "no exit code in step output"
SYSTEM_STEP_NA = "a platform tool's output carries no citations"
MISSING_CITATIONS = "missing citations"
CRITERIA_UNEVALUABLE = "criterion could not be evaluated on this step's output"

# An exit code is a decimal integer, nothing else: ``str.isdigit`` accepts
# superscripts and ``lstrip("-")`` accepts "--1", and ``int()`` then raises.
_EXIT_CODE_RE = re.compile(r"-?[0-9]+")
# What a JSON parser can raise on an agent's bytes. RecursionError is the one
# a narrow ``except JSONDecodeError`` misses (nesting past the C decoder's limit).
_PARSE_ERRORS = (json.JSONDecodeError, TypeError, ValueError, RecursionError)
# The fields of an MCP tool result (``CallToolResult``); a mapping with only
# these keys and a ``content`` list is the transport's wrapper, not the
# agent's own document, and is read block by block.
_MCP_RESULT_KEYS = frozenset({"content", "isError", "structuredContent", "_meta"})


def criteria_units(raw_output: Any) -> list[str]:
    """The texts a criterion reads for one step: one per content block (FR-7).

    The normalizer cuts plain-text content to 280 characters for the display
    card; a criterion judging that card misreads a long test-runner payload
    whose exit code sits past the cut. This reads the dispatch result —
    already credential-redacted by the pipeline — the way
    ``emerge.preimage.output_preimage`` sees it: a string as one unit, MCP
    content blocks (a list, or the raw-HTTP fallback's result wrapper) as one
    unit each, any other mapping as its JSON. Never raises.
    """
    try:
        if raw_output is None:
            return []
        if isinstance(raw_output, str):
            return [raw_output]
        if (
            isinstance(raw_output, dict)
            and isinstance(raw_output.get("content"), list)
            and set(raw_output) <= _MCP_RESULT_KEYS
        ):
            return [_block_text(item) for item in raw_output["content"]]
        if isinstance(raw_output, (list, tuple)):
            return [_block_text(item) for item in raw_output]
        return [_block_text(raw_output)]
    except Exception:
        return []


def criteria_text(raw_output: Any) -> str:
    """Every unit of *raw_output* as one text (tests and logs; never raises)."""
    return "\n".join(criteria_units(raw_output))


def _block_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(item, dict):
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
        return json.dumps(item, default=str)
    return str(item)


def has_valid_citations(content: str) -> bool:
    """True when *content* is JSON with a non-empty, well-formed citations list."""
    try:
        payload = json.loads(content)
    except _PARSE_ERRORS:
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
    unparseable content returns ``None``: the criterion is ``n/a`` on that step.
    Never raises: the content is the agent's.
    """
    try:
        payload = json.loads(content)
    except _PARSE_ERRORS:
        return None
    if not isinstance(payload, dict):
        return None
    for key in _EXIT_KEYS:
        raw = payload.get(key)
        if type(raw) is int:
            return raw
        if isinstance(raw, str) and _EXIT_CODE_RE.fullmatch(raw.strip()):
            return int(raw)
    return None


def evaluate_criteria(
    criteria: dict[str, Any], raw_output: Any
) -> dict[str, dict[str, str]]:
    """Per-criterion result for one step: ``{key: {"result", "detail"}}``.

    ``result`` is ``pass`` | ``fail`` | ``n/a``. Unknown keys are ``fail``
    (closed) so they can never be signed as accepted. A key declared ``false``
    is not a declaration and is omitted. A criterion whose evaluation raises
    on this output is ``fail`` (``CRITERIA_UNEVALUABLE``), never an exception.
    """
    units = criteria_units(raw_output)
    results: dict[str, dict[str, str]] = {}
    for key in criteria:
        if key not in SUPPORTED_CRITERIA:
            results[key] = {"result": "fail", "detail": f"unsupported criterion: {key}"}
            continue
        if not criteria.get(key):
            continue
        try:
            results[key] = _EVALUATORS[key](units)
        except Exception:
            results[key] = {"result": "fail", "detail": CRITERIA_UNEVALUABLE}
    return results


def _citations_required(units: list[str]) -> dict[str, str]:
    # citations in any block satisfy the step: an answer may sit beside a note
    ok = any(has_valid_citations(unit) for unit in units)
    return {
        "result": "pass" if ok else "fail",
        "detail": "ok" if ok else MISSING_CITATIONS,
    }


def _exit_zero(units: list[str]) -> dict[str, str]:
    # a nonzero code in any block fails the step; a code of 0 in one block
    # passes it only when no block reports otherwise
    codes = [c for c in (parse_exit_code(unit) for unit in units) if c is not None]
    nonzero = [c for c in codes if c != 0]
    if nonzero:
        return {"result": "fail", "detail": f"nonzero exit: {nonzero[0]}"}
    if codes:
        return {"result": "pass", "detail": "ok"}
    return {"result": NOT_APPLICABLE, "detail": NO_EXIT_CODE}


_EVALUATORS = {"citations_required": _citations_required, "exit_zero": _exit_zero}


def _summarise(per: dict[str, dict[str, str]]) -> dict[str, Any]:
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


def step_declared_acceptance(
    criteria: dict[str, Any], raw_output: Any
) -> dict[str, Any]:
    """The ``declared_acceptance`` step-metadata entry for an agent step.

    ``{"result": pass|fail|n/a, "detail": str, "criteria": {key: result}}`` —
    ``fail`` if any criterion failed on this step, ``n/a`` if no declared
    criterion applies to it, else ``pass``. The per-criterion map is what the
    run observer aggregates; ``result`` is the step's own summary.
    *raw_output* is the dispatch result as the pipeline holds it (redacted).
    """
    return _summarise(evaluate_criteria(criteria, raw_output))


def system_step_declared_acceptance(
    criteria: dict[str, Any], raw_output: Any
) -> dict[str, Any]:
    """The ``declared_acceptance`` entry for a platform system tool's step.

    ``exit_zero`` reads the step like any other (an exit code is an exit code
    wherever it appears). ``citations_required`` is ``n/a``: citations are a
    property of an agent's answer, and a platform tool (a checklist update, a
    clock read) never carries them — judging one would fail every cited turn
    that also touched a checklist. The run-level rule then decides on the steps
    a criterion applied to, and a run with none fails the criterion.
    """
    per = evaluate_criteria(
        {k: v for k, v in criteria.items() if k != "citations_required"}, raw_output
    )
    if criteria.get("citations_required"):
        per["citations_required"] = {"result": NOT_APPLICABLE, "detail": SYSTEM_STEP_NA}
    return _summarise({k: per[k] for k in criteria if k in per})


def evaluate_declared_criteria(
    criteria: dict[str, Any], raw_output: Any
) -> tuple[bool, str]:
    """Step-level ``(accepted, reason)``: accepted unless a criterion **failed**.

    A step on which a criterion is ``n/a`` is not a failure of that step; the
    run-level rule decides whether the run as a whole satisfied it.
    """
    entry = step_declared_acceptance(criteria, raw_output)
    return entry["result"] != "fail", str(entry["detail"])


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
