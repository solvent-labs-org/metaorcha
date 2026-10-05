"""Declared acceptance criteria on the platform: glue over ``emerge.criteria``.

The digest, the step rule and the run rule are the SDK's and only the SDK's
(AD-20, story 4.1): ``emerge.criteria`` evaluates a step's raw output block
by block, signs a parser fault as ``fail`` rather than raising, and folds the
steps into the one ``declared_acceptance`` verdict — for the platform and for
``orcha record`` alike. The names the pipeline, the system-tool node and the
tests read from here are re-exported unchanged.

What is the platform's own stays here: the system-tool reading of a step
(``citations_required`` is ``n/a`` on a platform tool's step), the run-level
criteria a routine declares (``RUN_CRITERIA``, story 2.4 — compared across
steps at seal, so the pipeline stamps their operands on every step), and the
step metadata the run observer reads (``criteria_step_meta``).
``exit_zero`` is chat-only for now: it and ``citations_required`` sign the one
``declared_acceptance`` verdict, and the routines pane names a criterion as
checked from that verdict, so a routine may not declare both until the
verdict says which criterion it judged.
"""

from __future__ import annotations

from typing import Any

from emerge.criteria import (  # noqa: F401 — the platform's readers import these here
    CRITERIA_UNEVALUABLE,
    MISSING_CITATIONS,
    NO_EXIT_CODE,
    NOT_APPLICABLE,
    SUPPORTED_CRITERIA,
    criteria_digest,
    criteria_text,
    criteria_units,
    evaluate_criteria,
    has_valid_citations,
    parse_exit_code,
    step_declared_acceptance,
    summarise_step,
)

# the re-exported names are this module's public API too (readers import them here)
__all__ = [
    "COUNTS_MATCH_OPERANDS",
    "CRITERIA_UNEVALUABLE",
    "MISSING_CITATIONS",
    "NOT_APPLICABLE",
    "NO_EXIT_CODE",
    "ROUTINE_CRITERIA",
    "RUN_CRITERIA",
    "SUPPORTED_CRITERIA",
    "SYSTEM_STEP_NA",
    "agent_step_meta",
    "criteria_digest",
    "criteria_step_meta",
    "criteria_text",
    "criteria_units",
    "evaluate_criteria",
    "evaluate_declared_criteria",
    "has_valid_citations",
    "parse_exit_code",
    "run_criteria_meta",
    "step_criteria",
    "step_declared_acceptance",
    "summarise_step",
    "system_step_declared_acceptance",
]

RUN_CRITERIA = frozenset({"counts_match"})
ROUTINE_CRITERIA = frozenset({"citations_required"}) | RUN_CRITERIA
COUNTS_MATCH_OPERANDS = frozenset({"left", "right", "left_path", "right_path", "key"})

SYSTEM_STEP_NA = "a platform tool's output carries no citations"


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
    return summarise_step({k: per[k] for k in criteria if k in per})


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


def agent_step_meta(
    criteria: dict[str, Any], routine_context: Any, raw_output: Any
) -> dict[str, Any]:
    """Everything the pipeline stamps on an agent step: the run metadata above
    plus this step's ``declared_acceptance``, judged on the step's own raw
    output by the one step rule.

    Stamped on successful, failed and declined steps alike. A failed call's
    error text carries no exit code (``exit_zero`` n/a) and no citations
    (``citations_required`` fail), so a run of only failed calls fails its
    declared acceptance rather than signing none — the reading the local
    journal gives the same bytes (story 4.1 parity). A routine declaring only
    a run-level criterion signs no per-step entry.
    """
    meta = criteria_step_meta(criteria, routine_context)
    per_step = step_criteria(criteria)
    if per_step:
        meta["declared_acceptance"] = step_declared_acceptance(per_step, raw_output)
    return meta
