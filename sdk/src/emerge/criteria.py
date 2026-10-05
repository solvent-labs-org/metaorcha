"""Declared acceptance criteria: the one digest and the one evaluation (AD-20).

Every producer of an ``orcha.run-attestation/v1`` envelope — the platform's
run observer and the local ``orcha record`` journal — reads this module and
nothing else for three things, so a plugin receipt and a bot receipt for the
same declared work carry the same bytes:

- **The digest.** ``policy_version`` is ``<policy>+criteria:<64-hex>`` when a
  turn declared criteria, else ``<policy>``: no new envelope field, both halves
  recoverable (``compose_policy_version`` / ``parse_policy_version``). The
  digest is the SHA-256 of the canonical criteria document — the same Python
  reference rendering the envelope uses (``canonical_json_bytes``).
- **The step rule.** Criteria are evaluated per step against the step's own
  output, read one content block at a time (``criteria_units``): a nonzero
  exit code in any block fails ``exit_zero``; citations in any block satisfy
  ``citations_required``. Each criterion is ``pass`` | ``fail`` | ``n/a`` on a
  step (``n/a``: the step carries nothing the criterion can read — a read or a
  patch step is not a failure of ``exit_zero``). Evaluation is total: the
  output is the agent's to choose, so a value that breaks a parser (a
  non-numeric "exit code", JSON nested past the recursion limit) is signed
  ``fail`` (``CRITERIA_UNEVALUABLE``), never raised — raising after dispatch
  would drop the step from the receipt on the agent's say-so.
- **The run rule.** Per criterion across the run's steps: ``fail`` if any
  applicable step failed; ``pass`` if at least one applied and none failed;
  a criterion no step was applicable to fails the run (``no step reported an
  exit code``) — declaring ``exit_zero`` and never running anything is not
  acceptance. One ``declared_acceptance`` verdict carries the outcome; a
  document whose every key is ``false`` declares nothing and signs none.

Unknown keys fail closed. ``SUPPORTED_CRITERIA`` is the vocabulary a step can
be judged on; a producer may layer its own run-level criteria on top (the
platform's ``counts_match``) but not evaluate those here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .run_attestation import canonical_json_bytes

SUPPORTED_CRITERIA = frozenset({"citations_required", "exit_zero"})
CRITERIA_MARKER = "+criteria:"

NOT_APPLICABLE = "n/a"
NO_EXIT_CODE = "no exit code in step output"
MISSING_CITATIONS = "missing citations"
CRITERIA_UNEVALUABLE = "criterion could not be evaluated on this step's output"
# declared_acceptance details for a declared criterion no step could be
# judged on; the routines pane reads these words as "declared, not evaluated"
NO_STEP_EXIT_CODE = "no step reported an exit code"
NO_STEP_APPLICABLE = "no step was applicable to"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CITATION_REQUIRED_FIELDS = ("chunk_id", "source_title", "excerpt")
_EXIT_KEYS = ("exit_code", "returncode", "exit")
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


# ── The digest ───────────────────────────────────────────────────────────────


def canonical_criteria_bytes(criteria: dict[str, Any]) -> bytes:
    """Stable bytes for a criteria document (the envelope's canonical form)."""
    return canonical_json_bytes(criteria)


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


# ── Reading a step's output ──────────────────────────────────────────────────


def criteria_units(raw_output: Any) -> list[str]:
    """The texts a criterion reads for one step: one per content block.

    A string is one unit; MCP content blocks (a list, or the raw-HTTP
    fallback's result wrapper) are one unit each; any other mapping is its
    JSON; ``None`` is nothing. Never raises: an output that cannot be read
    at all is ``[]`` here — the step rule reads it as unevaluable (a signed
    fail), not as a step with nothing to judge.
    """
    try:
        return _read_units(raw_output)
    except Exception:
        return []


def _read_units(raw_output: Any) -> list[str]:
    """``criteria_units`` without the net: raises on an unreadable output."""
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
            try:
                return int(raw)
            except ValueError:  # past int()'s digit limit: not an exit code
                return None
    return None


# ── The step rule ────────────────────────────────────────────────────────────


def evaluate_criteria(
    criteria: dict[str, Any], raw_output: Any
) -> dict[str, dict[str, str]]:
    """Per-criterion result for one step: ``{key: {"result", "detail"}}``.

    ``result`` is ``pass`` | ``fail`` | ``n/a``. Unknown keys are ``fail``
    (closed) so they can never be signed as accepted. A key declared ``false``
    is not a declaration and is omitted. A criterion whose evaluation raises
    on this output is ``fail`` (``CRITERIA_UNEVALUABLE``), never an exception.
    """
    try:
        units: list[str] | None = _read_units(raw_output)
    except Exception:
        # an output no block of which can be rendered is not "a step with no
        # exit code": every declared criterion is unevaluable on it
        units = None
    results: dict[str, dict[str, str]] = {}
    for key in criteria:
        if key not in SUPPORTED_CRITERIA:
            results[key] = {"result": "fail", "detail": f"unsupported criterion: {key}"}
            continue
        if not criteria.get(key):
            continue
        try:
            if units is None:
                raise ValueError(CRITERIA_UNEVALUABLE)
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


def summarise_step(per: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Fold per-criterion results into one step entry.

    ``{"result": pass|fail|n/a, "detail": str, "criteria": {key: result}}`` —
    ``fail`` if any criterion failed on this step, ``n/a`` if no declared
    criterion applies to it, else ``pass``. The per-criterion map is what the
    run rule aggregates; ``result`` is the step's own summary.
    """
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
    """The ``declared_acceptance`` step entry for one step's raw output."""
    return summarise_step(evaluate_criteria(criteria, raw_output))


# ── The run rule ─────────────────────────────────────────────────────────────


def run_declared_acceptance(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One run-level ``declared_acceptance`` verdict from the steps' entries.

    *entries* are the per-step entries of :func:`step_declared_acceptance`
    (a producer passes every step's, in order). Returns the verdict
    ``{"check": "declared_acceptance", "result", "detail"}`` or ``None`` when
    nothing was declared. Entries sealed before per-criterion results existed
    (no ``criteria`` map) fall back to fail-wins-else-last.
    """
    seen = [e for e in entries if e]
    if not seen:
        return None
    if all(isinstance(e.get("criteria"), dict) for e in seen):
        keys: list[str] = []
        for e in seen:
            for k in e["criteria"]:
                if k not in keys:
                    keys.append(k)
        if not keys:
            return None
        per_key: dict[str, str] = {}
        for key in keys:
            results = [e["criteria"][key] for e in seen if key in e["criteria"]]
            if "fail" in results:
                per_key[key] = "fail"
            elif "pass" in results:
                per_key[key] = "pass"
            else:
                per_key[key] = NOT_APPLICABLE
        failing = [k for k, v in per_key.items() if v == "fail"]
        unapplied = [k for k, v in per_key.items() if v == NOT_APPLICABLE]
        if failing:
            key = failing[0]
            step_detail = next(
                (
                    str(e.get("detail"))
                    for e in seen
                    if e["criteria"].get(key) == "fail" and e.get("detail")
                ),
                "fail",
            )
            result, detail = "fail", f"{key}: {step_detail}"
        elif unapplied:
            key = unapplied[0]
            result = "fail"
            detail = (
                NO_STEP_EXIT_CODE
                if key == "exit_zero"
                else f"{NO_STEP_APPLICABLE} {key}"
            )
        else:
            result, detail = "pass", "ok"
        return {"check": "declared_acceptance", "result": result, "detail": detail}
    failed = next((item for item in seen if item.get("result") == "fail"), None)
    chosen = failed or seen[-1]
    result = chosen.get("result")
    if result not in ("pass", "fail"):
        return None
    entry: dict[str, Any] = {"check": "declared_acceptance", "result": result}
    detail = chosen.get("detail")
    if detail:
        entry["detail"] = str(detail)
    return entry
