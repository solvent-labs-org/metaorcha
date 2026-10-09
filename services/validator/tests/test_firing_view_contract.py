"""Contract: the pane's "checked" rules match what the validator signs (story 2.5).

``common.utils.src.firing_view`` says "checked: counts_match" only where the
signed ``counts_match`` verdict compared something. It recognises "nothing
was compared" by the validator's own detail strings, copied as literals;
this job runs on changes to ``validator`` or ``common``, so a string renamed
on either side fails here rather than turning a never-evaluated check into
"checked" on the pane.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# ``common.*`` is a namespace package, importable from the repository root
# only (as services/gateway/tests/conftest.py does).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from conftest import FakeDB, _step_result  # noqa: E402
from validator import counts_match as cm  # noqa: E402
from validator.counts_match import evaluate_counts_match  # noqa: E402
from validator.run_observer import RunAttestationObserver  # noqa: E402

from common.utils.src import firing_view  # noqa: E402

GH = "did:orcha:agent:gh-1a2b3c4d"
NOTION = "did:orcha:agent:notion-5e6f7a8b"
OPERANDS = {
    "left": f"{GH}#list_pull_requests",
    "left_path": "/total_count",
    "right": f"{NOTION}#query_database",
    "right_path": "/results",
    "key": "open_prs",
}


def _evaluated(verdict: dict[str, Any]) -> bool:
    return firing_view.evaluated("counts_match", {"verdicts": [verdict]})


def _step(tool: str, output: Any, *, success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(tool=tool, output=output, success=success, has_preimage=True)


def test_the_not_evaluated_details_are_the_validators() -> None:
    details = firing_view.NOT_EVALUATED_DETAILS[cm.CHECK]
    assert cm._NO_OPERANDS in details
    assert cm._NOT_EVALUATED in details


def test_the_not_evaluated_markers_are_the_validators() -> None:
    markers = firing_view.NOT_EVALUATED_MARKERS[cm.CHECK]
    assert f"unreadable({cm.NEVER_CALLED})" in markers
    assert f"unreadable({cm.NO_PATH})" in markers


@pytest.mark.parametrize(
    "operands", [None, {}, {"left": OPERANDS["left"]}, {"left": 1, "right": 2}]
)
def test_no_operands_is_signed_fail_and_read_as_not_evaluated(operands: Any) -> None:
    verdict = evaluate_counts_match([], operands)
    assert verdict["result"] == "fail"
    assert _evaluated(verdict) is False


def test_sources_never_called_are_read_as_not_evaluated() -> None:
    verdict = evaluate_counts_match([], dict(OPERANDS))
    assert verdict["result"] == "fail"
    assert "unreadable(never called)" in verdict["detail"]
    assert _evaluated(verdict) is False


def test_one_source_never_called_is_read_as_not_evaluated() -> None:
    steps = [_step(OPERANDS["left"], {"total_count": 3})]
    verdict = evaluate_counts_match(steps, dict(OPERANDS))
    assert verdict["detail"].startswith("left=3 right=unreadable(never called) ")
    assert _evaluated(verdict) is False


def test_a_side_with_no_path_is_read_as_not_evaluated() -> None:
    operands = {k: v for k, v in OPERANDS.items() if k != "right_path"}
    steps = [
        _step(OPERANDS["left"], {"total_count": 3}),
        _step(OPERANDS["right"], {"results": [1, 2, 3]}),
    ]
    verdict = evaluate_counts_match(steps, operands)
    assert "unreadable(no path declared)" in verdict["detail"]
    assert _evaluated(verdict) is False


def test_an_evaluator_fault_is_read_as_not_evaluated(monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(cm, "_side", boom)
    verdict = evaluate_counts_match([], dict(OPERANDS))
    assert verdict["detail"] == cm._NOT_EVALUATED
    assert _evaluated(verdict) is False


@pytest.mark.parametrize(("right", "result"), [(3, "pass"), (2, "fail")])
def test_a_real_comparison_is_read_as_evaluated(right: int, result: str) -> None:
    steps = [
        _step(OPERANDS["left"], {"total_count": 3}),
        _step(OPERANDS["right"], {"results": list(range(right))}),
    ]
    verdict = evaluate_counts_match(steps, dict(OPERANDS))
    assert verdict["result"] == result
    assert _evaluated(verdict) is True


def test_a_source_read_but_not_a_count_is_a_check_that_ran() -> None:
    # The source was called and its output judged: the check ran, and failed.
    steps = [
        _step(OPERANDS["left"], "not json at all"),
        _step(OPERANDS["right"], {"results": [1]}),
    ]
    verdict = evaluate_counts_match(steps, dict(OPERANDS))
    assert "unreadable(not JSON)" in verdict["detail"]
    assert _evaluated(verdict) is True


@pytest.mark.asyncio
async def test_every_criterion_maps_to_a_check_the_observer_signs() -> None:
    meta = {
        "criteria_digest": "a" * 64,
        "run_criteria": {"counts_match": dict(OPERANDS)},
        "declared_acceptance": {"result": "fail", "detail": "missing citations"},
    }
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_step_complete(
        _step_result(
            "c1",
            agent_id=GH,
            capability_id="list_pull_requests",
            tool_name="list_pull_requests",
            content="{}",
            output_preimage={"total_count": 1},
            metadata=meta,
        )
    )
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()

    signed = {v["check"] for v in envelope["verdicts"]}
    assert set(firing_view.VERDICT_FOR_CRITERION.values()) <= signed
    assert firing_view.VERDICT_FOR_CRITERION["counts_match"] == cm.CHECK
    # ...and the pane reads this very envelope: the citation check ran; the
    # count's right source was never called, so it is not called checked.
    criteria = dict.fromkeys(firing_view.VERDICT_FOR_CRITERION, True)
    kind, _ = firing_view.checks_view(criteria, envelope)
    assert kind == "not_evaluated"  # right was never called
    assert firing_view.checks_view({"citations_required": True}, envelope) == (
        "checked",
        "checked: citations_required",
    )
