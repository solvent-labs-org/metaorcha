"""Story 2.4 / FR-25 / AD-19: ``counts_match`` — two sources, one signed verdict.

The evaluator reads each declared source's last successful call (its AD-16
pre-image), walks the operand's JSON Pointer to a count, and signs pass only
when both counts are readable and equal. Unreadable is fail, never skipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeDB, _step_result
from emerge.run_attestation import verify_run_attestation
from validator import counts_match as cm
from validator.counts_match import evaluate_counts_match, read_count
from validator.run_observer import RunAttestationObserver

GH = "did:orcha:agent:gh-1a2b3c4d"
NOTION = "did:orcha:agent:notion-5e6f7a8b"
LEFT = f"{GH}#list_pull_requests"
RIGHT = f"{NOTION}#query_database"
OPERANDS = {
    "left": LEFT,
    "left_path": "/total_count",
    "right": RIGHT,
    "right_path": "/results",
    "key": "open_prs",
}
SOURCES = (
    f"left_source={LEFT} left_path=/total_count "
    f"right_source={RIGHT} right_path=/results"
)


def _step(tool: str, output: Any, *, success: bool = True, has_preimage: bool = True):
    return SimpleNamespace(
        tool=tool, output=output, success=success, has_preimage=has_preimage
    )


def _rows(n: int) -> dict[str, Any]:
    return {"results": [{"id": i} for i in range(n)]}


# --- read_count ------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "path", "expected"),
    [
        ({"total_count": 11}, "/total_count", 11),
        ({"results": [1, 2, 3]}, "/results", 3),
        ("11", "", 11),
        ({"text": '{"total_count": 7}'}, "/text/total_count", 7),
        # a multi-block MCP pre-image: JSON text inside a list of blocks
        (['{"total_count": 4}', "page 1"], "/0/total_count", 4),
        ({"a/b": 5}, "/a~1b", 5),
        ({"a~b": 6}, "/a~0b", 6),
        ({"total_count": 0}, "/total_count", 0),
    ],
)
def test_read_count_reads_a_count(output: Any, path: str, expected: int) -> None:
    assert read_count(output, path, has_preimage=True) == (expected, "")


@pytest.mark.parametrize(
    ("output", "path", "reason"),
    [
        ({"n": True}, "/n", cm.NOT_A_COUNT),
        ({"n": 11.0}, "/n", cm.NOT_A_COUNT),
        ({"n": -1}, "/n", cm.NOT_A_COUNT),
        ({"n": {"x": 1}}, "/n", cm.NOT_A_COUNT),
        ({"n": None}, "/n", cm.NOT_A_COUNT),
        ("true", "", cm.NOT_A_COUNT),
        ({"n": 1}, "/missing", cm.PATH_NOT_FOUND),
        ([1, 2], "/2", cm.PATH_NOT_FOUND),
        ([1, 2], "/first", cm.PATH_NOT_FOUND),
        ([1, 2], "/01", cm.PATH_NOT_FOUND),
        (7, "/n", cm.PATH_NOT_FOUND),
        ({"n": 1}, "n", cm.PATH_NOT_FOUND),
        ("Error-ish text", "/n", cm.NOT_JSON),
        ({"n": "rate limited"}, "/n", cm.NOT_JSON),
    ],
)
def test_read_count_names_why_it_cannot(output: Any, path: str, reason: str) -> None:
    assert read_count(output, path, has_preimage=True) == (None, reason)


def test_the_display_copy_is_never_counted() -> None:
    assert read_count({"total_count": 11}, "/total_count", has_preimage=False) == (
        None,
        cm.NO_PREIMAGE,
    )


# --- evaluate_counts_match --------------------------------------------------


def test_equal_counts_pass() -> None:
    entry = evaluate_counts_match(
        [_step(LEFT, {"total_count": 11}), _step(RIGHT, _rows(11))], OPERANDS
    )
    assert entry == {
        "check": "counts_match",
        "result": "pass",
        "detail": f"left=11 right=11 key=open_prs {SOURCES}",
    }


def test_different_counts_fail_naming_both() -> None:
    entry = evaluate_counts_match(
        [_step(LEFT, {"total_count": 11}), _step(RIGHT, _rows(10))], OPERANDS
    )
    assert entry == {
        "check": "counts_match",
        "result": "fail",
        "detail": f"left=11 right=10 key=open_prs {SOURCES}",
    }


def test_an_undeclared_key_is_left_out_of_the_detail() -> None:
    operands = {k: v for k, v in OPERANDS.items() if k != "key"}
    entry = evaluate_counts_match(
        [_step(LEFT, {"total_count": 3}), _step(RIGHT, _rows(3))], operands
    )
    assert entry["result"] == "pass"
    assert entry["detail"] == f"left=3 right=3 {SOURCES}"


def test_a_side_without_a_path_is_unreadable() -> None:
    entry = evaluate_counts_match(
        [_step(LEFT, "3"), _step(RIGHT, [1, 2, 3])], {"left": LEFT, "right": RIGHT}
    )
    assert entry == {
        "check": "counts_match",
        "result": "fail",
        "detail": "left=unreadable(no path declared) "
        "right=unreadable(no path declared) "
        f"left_source={LEFT} right_source={RIGHT}",
    }


def test_multi_block_outputs_are_counted_by_their_data_not_their_blocks() -> None:
    # Two MCP content blocks each: counted whole, both would be 2 and pass.
    left = ["Found issues", '{"total_count": 11}']
    right = ["Query ok", '{"results": [1, 2, 3]}']
    steps = [_step(LEFT, left), _step(RIGHT, right)]
    unpathed = evaluate_counts_match(steps, {"left": LEFT, "right": RIGHT})
    assert unpathed["result"] == "fail"
    pathed = evaluate_counts_match(
        steps,
        {
            "left": LEFT,
            "left_path": "/1/total_count",
            "right": RIGHT,
            "right_path": "/1/results",
        },
    )
    assert pathed["result"] == "fail"
    assert pathed["detail"].startswith("left=11 right=3 ")


@pytest.mark.parametrize(
    ("steps", "right"),
    [
        (
            [_step(LEFT, {"total_count": 11}), _step(RIGHT, {"error": "rate limited"})],
            "unreadable(path not found)",
        ),
        (
            [_step(LEFT, {"total_count": 11}), _step(RIGHT, {"results": "n/a"})],
            "unreadable(not JSON)",
        ),
        (
            [_step(LEFT, {"total_count": 11}), _step(RIGHT, {"results": {"a": 1}})],
            "unreadable(not a count)",
        ),
        ([_step(LEFT, {"total_count": 11})], "unreadable(never called)"),
        (
            [_step(LEFT, {"total_count": 11}), _step(RIGHT, _rows(11), success=False)],
            "unreadable(no successful call)",
        ),
        (
            [
                _step(LEFT, {"total_count": 11}),
                _step(RIGHT, _rows(11), has_preimage=False),
            ],
            "unreadable(no pre-image)",
        ),
    ],
)
def test_an_unreadable_side_fails(steps: list[Any], right: str) -> None:
    entry = evaluate_counts_match(steps, OPERANDS)
    assert entry["result"] == "fail"
    assert entry["detail"] == f"left=11 right={right} key=open_prs {SOURCES}"


def test_both_sides_unreadable_fail() -> None:
    entry = evaluate_counts_match([], OPERANDS)
    assert entry["result"] == "fail"
    assert entry["detail"].startswith(
        "left=unreadable(never called) right=unreadable(never called) "
    )


def test_the_last_successful_read_wins() -> None:
    steps = [
        _step(LEFT, {"total_count": 9}),
        _step(RIGHT, _rows(11)),
        _step(LEFT, {"total_count": 11}),
    ]
    assert evaluate_counts_match(steps, OPERANDS)["result"] == "pass"


def test_a_failed_last_call_is_ignored() -> None:
    steps = [
        _step(LEFT, {"total_count": 11}),
        _step(LEFT, "Error: timeout", success=False),
        _step(RIGHT, _rows(11)),
    ]
    assert evaluate_counts_match(steps, OPERANDS)["result"] == "pass"


def test_a_successful_error_payload_does_not_fall_back_to_an_earlier_read() -> None:
    steps = [
        _step(LEFT, {"total_count": 11}),
        _step(LEFT, "Error-ish text"),
        _step(RIGHT, _rows(11)),
    ]
    entry = evaluate_counts_match(steps, OPERANDS)
    assert entry["result"] == "fail"
    assert entry["detail"].startswith("left=unreadable(not JSON) right=11 ")


@pytest.mark.parametrize(
    "operands", [{}, None, {"left": LEFT}, {"left": 1, "right": 2}]
)
def test_missing_operands_fail(operands: Any) -> None:
    assert evaluate_counts_match([], operands) == {
        "check": "counts_match",
        "result": "fail",
        "detail": "no operands declared",
    }


def test_a_hostile_operand_keeps_the_detail_printable_ascii() -> None:
    entry = evaluate_counts_match([], {**OPERANDS, "key": "prés\nx"})
    assert entry["result"] == "fail"
    assert all(" " <= ch <= "~" for ch in entry["detail"])
    assert "key=pr?s?x" in entry["detail"]


def test_an_evaluator_fault_fails_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cm, "read_count", boom)
    entry = evaluate_counts_match(
        [_step(LEFT, {"total_count": 11}), _step(RIGHT, _rows(11))], OPERANDS
    )
    assert entry == {
        "check": "counts_match",
        "result": "fail",
        "detail": "counts_match could not be evaluated",
    }


# --- through the run observer ----------------------------------------------

DIGEST = "a" * 64


def _call(
    call_id: str,
    did: str,
    capability: str,
    preimage: Any,
    *,
    success: bool = True,
    metadata: dict[str, Any] | None = None,
):
    return _step_result(
        call_id,
        agent_id=did,
        capability_id=capability,
        tool_name=capability,
        success=success,
        content=str(preimage)[:280],
        output_preimage=preimage,
        metadata=metadata
        if metadata is not None
        else {
            "criteria_digest": DIGEST,
            "run_criteria": {"counts_match": dict(OPERANDS)},
        },
    )


async def _seal(*records: Any) -> dict[str, Any]:
    observer = RunAttestationObserver(db=FakeDB())
    for record in records:
        await observer.on_step_complete(record)
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


def _checks(envelope: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [v for v in envelope["verdicts"] if v["check"] == name]


@pytest.mark.asyncio
@pytest.mark.parametrize(("right", "result"), [(11, "pass"), (10, "fail")])
async def test_the_envelope_signs_one_counts_match_verdict(
    right: int, result: str
) -> None:
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", {"total_count": 11}),
        _call("c2", NOTION, "query_database", _rows(right)),
    )
    assert verify_run_attestation(envelope).valid
    assert _checks(envelope, "counts_match") == [
        {
            "check": "counts_match",
            "result": result,
            "detail": f"left=11 right={right} key=open_prs {SOURCES}",
        }
    ]
    assert envelope["policy_version"].endswith(f"+criteria:{DIGEST}")


@pytest.mark.asyncio
async def test_counts_match_sits_after_declared_acceptance_and_is_independent() -> None:
    meta = {
        "criteria_digest": DIGEST,
        "run_criteria": {"counts_match": dict(OPERANDS)},
        "declared_acceptance": {"result": "fail", "detail": "missing citations"},
    }
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", {"total_count": 11}, metadata=meta),
        _call("c2", NOTION, "query_database", _rows(11), metadata=meta),
    )
    checks = [v["check"] for v in envelope["verdicts"]]
    assert checks.index("declared_acceptance") < checks.index("counts_match")
    assert _checks(envelope, "declared_acceptance")[0]["result"] == "fail"
    assert _checks(envelope, "counts_match")[0]["result"] == "pass"


@pytest.mark.asyncio
async def test_a_run_without_run_criteria_signs_no_counts_match() -> None:
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", {"total_count": 11}, metadata={}),
    )
    assert _checks(envelope, "counts_match") == []


@pytest.mark.asyncio
async def test_a_run_whose_sources_all_failed_still_signs_the_fail() -> None:
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", "Error: 502", success=False),
        _call("c2", NOTION, "query_database", "Error: 502", success=False),
    )
    assert verify_run_attestation(envelope).valid
    (entry,) = _checks(envelope, "counts_match")
    assert entry["result"] == "fail"
    assert entry["detail"].startswith(
        "left=unreadable(no successful call) right=unreadable(no successful call) "
    )


@pytest.mark.asyncio
async def test_a_step_without_its_pre_image_is_not_counted() -> None:
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", {"total_count": 11}),
        _call("c2", NOTION, "query_database", None),
    )
    (entry,) = _checks(envelope, "counts_match")
    assert entry["result"] == "fail"
    assert "right=unreadable(no pre-image)" in entry["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("right", "result"), [(11, "pass"), (10, "fail")])
async def test_the_pipelines_own_metadata_reaches_the_verdict(
    right: int, result: str
) -> None:
    """Contract: the SuperAgent writes the step metadata this observer reads.

    Built with the pipeline's own writer (``criteria_step_meta``), so a key
    renamed on either side fails here; this job runs on changes to either.
    """
    from superagent.middleware.criteria import criteria_digest, criteria_step_meta

    criteria = {"counts_match": True}
    meta = criteria_step_meta(
        criteria, {"criteria_operands": {"counts_match": dict(OPERANDS)}}
    )
    envelope = await _seal(
        _call("c1", GH, "list_pull_requests", {"total_count": 11}, metadata=meta),
        _call("c2", NOTION, "query_database", _rows(right), metadata=meta),
    )
    (entry,) = _checks(envelope, "counts_match")
    assert entry["result"] == result
    assert envelope["policy_version"].endswith(f"+criteria:{criteria_digest(criteria)}")
