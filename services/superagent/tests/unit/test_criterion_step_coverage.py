"""Story 3.1: the call a criterion judges is a step the receipt covers (FR-7).

Carried onto the stack from draft #78, corrected where the stack moved:

- criteria read the agent's own output (``criteria_text``), never the
  280-character card — including a dict or MCP content-block result, which
  #78 still read off the card;
- a platform system tool's call is a step (``steps_root`` covers it), with no
  structural verdict and ``citations_required`` ``n/a`` on it, so a cited turn
  that also touched a checklist is not refused;
- a routine's run-level metadata rides on system steps too, so a
  ``counts_match`` firing never signs "unsupported criterion".

End to end where it matters: the real pipeline and the real system-tool node
emit the steps, the real run-attestation observer seals them, the published
SDK verifies the envelope and the real gate judges it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from emerge.run_attestation import verify_run_attestation
from langchain_core.messages import AIMessage
from superagent.config import settings
from superagent.middleware.criteria import (
    CRITERIA_UNEVALUABLE,
    MISSING_CITATIONS,
    NO_EXIT_CODE,
    NOT_APPLICABLE,
    SYSTEM_STEP_NA,
    agent_step_meta,
    criteria_digest,
    criteria_text,
    criteria_units,
    parse_exit_code,
    step_declared_acceptance,
    system_step_declared_acceptance,
)
from superagent.middleware.observers import (
    CompositeObserver,
    NoOpObserver,
    StepResult,
    set_observer,
)
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.middleware.system_steps import (
    SYSTEM_TOOL_AGENT_DID,
    system_result_success,
)
from superagent.nodes.execute_agent_calls import execute_agent_calls_node
from superagent.pricing import settle_gate
from superagent.pricing.settle_gate import SettlementGateObserver
from superagent.system_tools.registry import register_all_system_tools

from common.utils.src import firing_view

from .test_routine_scheduler import DB

RUNNER = "did:orcha:agent:test-runner"
SESSION = "sess-31"

# A test runner's JSON whose exit code sits past the card's 280-character cut.
_TAIL = "." * 400 + "\n1 passed in 0.02s\n"


def _runner_output(exit_code: int) -> dict[str, Any]:
    return {"command": "pytest -q", "stdout_tail": _TAIL, "exit_code": exit_code}


CITED = json.dumps(
    {
        "answer": "x" * 300,
        "citations": [{"chunk_id": "c1", "source_title": "Doc", "excerpt": "e"}],
    }
)


class _TextBlock:
    """An MCP ``TextContent``: the shape ``mcp_handler`` returns for many blocks."""

    def __init__(self, text: str) -> None:
        self.text = text


# ---------------------------------------------------------------------------
# criteria_text: the bytes a criterion reads
# ---------------------------------------------------------------------------


def test_criteria_text_renders_every_result_shape_uncapped() -> None:
    long = json.dumps(_runner_output(1))
    assert len(long) > 280
    assert criteria_text(long) == long
    assert criteria_text(None) == ""
    assert json.loads(criteria_text(_runner_output(1)))["exit_code"] == 1
    assert criteria_text([_TextBlock(long)]) == long
    assert criteria_text([{"type": "text", "text": long}]) == long
    assert criteria_text([_TextBlock("a"), {"type": "text", "text": "b"}]) == "a\nb"
    assert criteria_text(42) == "42"

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert criteria_text(Hostile()) == ""


def test_exit_zero_is_n_a_without_an_exit_code_and_fails_on_nonzero() -> None:
    assert step_declared_acceptance({"exit_zero": True}, "patched 3 files") == {
        "result": NOT_APPLICABLE,
        "detail": NO_EXIT_CODE,
        "criteria": {"exit_zero": NOT_APPLICABLE},
    }
    assert step_declared_acceptance(
        {"exit_zero": True}, json.dumps({"exit_code": 2})
    ) == {
        "result": "fail",
        "detail": "nonzero exit: 2",
        "criteria": {"exit_zero": "fail"},
    }


def test_a_system_step_reads_exit_codes_but_never_citations() -> None:
    both = {"citations_required": True, "exit_zero": True}
    assert system_step_declared_acceptance(both, '{"now": "2026-10-05"}') == {
        "result": NOT_APPLICABLE,
        "detail": SYSTEM_STEP_NA,
        "criteria": {"citations_required": NOT_APPLICABLE, "exit_zero": NOT_APPLICABLE},
    }
    assert system_step_declared_acceptance(both, json.dumps({"exit_code": 1})) == {
        "result": "fail",
        "detail": "nonzero exit: 1",
        "criteria": {"citations_required": NOT_APPLICABLE, "exit_zero": "fail"},
    }
    # an unknown key still fails closed on a system step
    assert system_step_declared_acceptance({"made_up": True}, "")["result"] == "fail"


# A result is read block by block: an MCP list, or the raw-HTTP fallback's
# CallToolResult wrapper (``mcp_handler._call_sse_raw`` returns resp.json()).
_WRAPPED_FAIL = {
    "content": [{"type": "text", "text": json.dumps({"exit_code": 1})}],
    "isError": True,
}
_MIXED_BLOCKS = [
    _TextBlock("3 failed, 9 passed"),
    _TextBlock(json.dumps({"exit_code": 1})),
]


def test_criteria_units_reads_blocks_and_the_mcp_wrapper_one_at_a_time() -> None:
    assert criteria_units(_MIXED_BLOCKS) == ["3 failed, 9 passed", '{"exit_code": 1}']
    assert criteria_units(_WRAPPED_FAIL) == ['{"exit_code": 1}']
    # an agent's own document that happens to carry a "content" list is not
    # the transport's wrapper: it is read whole
    own = {"content": [{"type": "text", "text": "x"}], "citations": []}
    assert criteria_units(own) == [json.dumps(own, default=str)]
    assert criteria_units(None) == []


@pytest.mark.parametrize(
    ("raw", "result", "detail"),
    [
        (_MIXED_BLOCKS, "fail", "nonzero exit: 1"),
        (_WRAPPED_FAIL, "fail", "nonzero exit: 1"),
        # a zero in one block passes only when no block says otherwise
        (
            [_TextBlock('{"exit_code": 0}'), _TextBlock('{"exit_code": 2}')],
            "fail",
            "nonzero exit: 2",
        ),
        ([_TextBlock("done"), _TextBlock('{"exit_code": 0}')], "pass", "ok"),
        # a string code is a code; a decoy that only looks like one is not
        ('{"exit_code": "1"}', "fail", "nonzero exit: 1"),
        ('{"exit_code": " 0 "}', "pass", "ok"),
        ('{"exit_code": "--1"}', NOT_APPLICABLE, NO_EXIT_CODE),
        ('{"exit_code": "\u00b2"}', NOT_APPLICABLE, NO_EXIT_CODE),
        ('{"exit_code": 1.0}', NOT_APPLICABLE, NO_EXIT_CODE),
    ],
)
def test_exit_zero_reads_every_block_and_only_real_codes(raw, result, detail) -> None:
    assert step_declared_acceptance({"exit_zero": True}, raw) == {
        "result": result,
        "detail": detail,
        "criteria": {"exit_zero": result},
    }


def test_citations_in_any_block_satisfy_the_step() -> None:
    beside = [_TextBlock("Here is what I found."), _TextBlock(CITED)]
    assert (
        step_declared_acceptance({"citations_required": True}, beside)["result"]
        == "pass"
    )
    assert step_declared_acceptance({"citations_required": True}, _WRAPPED_FAIL) == {
        "result": "fail",
        "detail": MISSING_CITATIONS,
        "criteria": {"citations_required": "fail"},
    }


_DEEP = "[" * 200_000 + "]" * 200_000  # past the JSON decoder's recursion limit


@pytest.mark.parametrize("raw", [_DEEP, [_TextBlock(_DEEP)], {"exit_code": _DEEP}])
def test_evaluation_never_raises_on_the_agents_bytes(raw) -> None:
    # the output is the agent's: a parser it breaks must not drop the step
    # from the receipt (pipeline step 5.5 runs after dispatch)
    assert parse_exit_code(_DEEP) is None
    both = {"citations_required": True, "exit_zero": True}
    entry = step_declared_acceptance(both, raw)
    assert entry["result"] in ("fail", NOT_APPLICABLE)
    assert system_step_declared_acceptance(both, raw)["result"] in (
        "fail",
        NOT_APPLICABLE,
    )


def test_an_evaluator_fault_is_signed_fail_not_raised(monkeypatch) -> None:
    # the evaluators live in the SDK (AD-20); the platform reads them from there
    from emerge import criteria as mod

    def boom(_units):
        raise RuntimeError("evaluator bug")

    monkeypatch.setitem(mod._EVALUATORS, "exit_zero", boom)
    assert step_declared_acceptance({"exit_zero": True}, '{"exit_code": 0}') == {
        "result": "fail",
        "detail": CRITERIA_UNEVALUABLE,
        "criteria": {"exit_zero": "fail"},
    }


def test_a_key_declared_false_is_not_evaluated_and_signs_nothing() -> None:
    assert step_declared_acceptance({"citations_required": False}, "no cites") == {
        "result": "pass",
        "detail": "ok",
        "criteria": {},
    }


# ---------------------------------------------------------------------------
# The pipeline reads the agent's bytes, not the card
# ---------------------------------------------------------------------------


class _FakePreFlight:
    def __init__(self, _vault: Any) -> None:
        pass

    async def run(self, **_kwargs: Any) -> dict[str, Any]:
        return {"manifest": {"transport": {}}, "headers": {}, "resolved_env": None}


async def _agent_step(
    state: dict[str, Any], raw: Any, *, call_id: str, dispatch: Any = None
) -> dict:
    with (
        patch("superagent.middleware.pipeline.PreFlightManager", _FakePreFlight),
        patch(
            "superagent.middleware.pipeline.InputGuard.validate",
            side_effect=lambda args, _schema: args,
        ),
        patch.object(
            ExecutionMiddleware, "_get_capability_schema", AsyncMock(return_value=None)
        ),
        patch.object(
            ExecutionMiddleware, "_dispatch", dispatch or AsyncMock(return_value=raw)
        ),
        # a free agent: no Prisma connect to the payment row
        patch.object(
            ExecutionMiddleware,
            "_resolve_base_fee",
            AsyncMock(return_value=Decimal("0")),
        ),
        patch("superagent.vault.client.VaultClient"),
    ):
        return await ExecutionMiddleware(state=state).execute(
            agent_id=RUNNER,
            capability_id="run_tests",
            protocol="A2A",
            tool_name="delegate__did_orcha_agent_test-runner",
            args={"task": "run pytest -q"},
            call_id=call_id,
            config={"configurable": {}},
        )


@pytest.fixture()
def recorded():
    seen: list[StepResult] = []

    class Recorder:
        async def on_step_complete(self, record: StepResult) -> None:
            seen.append(record)

    set_observer(Recorder())
    yield seen
    set_observer(NoOpObserver())


def _state(criteria: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "session_id": SESSION,
        "_declared_criteria": criteria,
        **extra,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # a long passing output is not refused: exit_code 0 sits past the cut
        (json.dumps(_runner_output(0)), "pass"),
        # a long failing output names the real reason, not "no exit code"
        (json.dumps(_runner_output(1)), "fail"),
        # the raw-HTTP MCP fallback returns a dict: #78 read its card
        (_runner_output(0), "pass"),
        (_runner_output(1), "fail"),
        # one MCP content block
        ([{"type": "text", "text": json.dumps(_runner_output(1))}], "fail"),
    ],
)
async def test_criteria_read_the_agent_bytes_not_the_card(
    recorded, raw: Any, expected: str
) -> None:
    result = await _agent_step(_state({"exit_zero": True}), raw, call_id="c-long")
    assert len(result["content"]) <= 280  # the card is still capped (display)
    (step,) = recorded
    entry = step.metadata["declared_acceptance"]
    assert entry["result"] == expected, entry
    assert entry["detail"] == ("ok" if expected == "pass" else "nonzero exit: 1")


@pytest.mark.parametrize(
    "raw", ['{"exit_code": "--1"}', _DEEP, [_TextBlock(_DEEP)], _MIXED_BLOCKS]
)
async def test_a_dispatched_call_is_a_step_whatever_its_bytes(recorded, raw) -> None:
    # an output chosen to break the parser must not remove the call from
    # the receipt: the step is emitted and the criterion is fail or n/a
    both = {"citations_required": True, "exit_zero": True}
    result = await _agent_step(_state(both), raw, call_id="c-hostile")
    assert not result["content"].startswith("Error:")
    (step,) = recorded
    assert step.call_id == "c-hostile"
    assert step.metadata["declared_acceptance"]["result"] in ("fail", NOT_APPLICABLE)


async def test_a_failed_dispatch_is_judged_on_its_error_text_like_any_step(
    recorded,
) -> None:
    # story 4.1 parity: the local journal judges a failed call on the bytes
    # it has; so does the platform. "Error: ..." carries no exit code (n/a)
    # and no citations (fail), so a run of only failed calls fails its
    # declared acceptance rather than signing none.
    both = {"citations_required": True, "exit_zero": True}
    with pytest.raises(RuntimeError, match="connection refused"):
        await _agent_step(
            _state(both),
            None,
            call_id="c-failed",
            dispatch=AsyncMock(side_effect=RuntimeError("connection refused")),
        )
    (step,) = recorded
    assert step.success is False and step.content.startswith("Error:")
    assert step.metadata["criteria_digest"] == criteria_digest(both)
    assert step.metadata["declared_acceptance"] == step_declared_acceptance(
        both, step.content
    )
    assert step.metadata["declared_acceptance"]["criteria"] == {
        "citations_required": "fail",
        "exit_zero": NOT_APPLICABLE,
    }
    # a declined call is stamped the same way (the scope gate's text is the
    # step's output); a routine declaring only a run-level criterion is not
    declined = agent_step_meta(both, None, "declined: write outside scope")
    assert declined["declared_acceptance"]["result"] == "fail"
    assert "declared_acceptance" not in agent_step_meta(
        {"counts_match": True}, None, "Error: x"
    )


# ---------------------------------------------------------------------------
# A system tool's call is a step
# ---------------------------------------------------------------------------


def _system_call(tool: str, state: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        **state,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": tool, "args": {}, "id": call_id, "type": "tool_call"}
                ],
            )
        ],
        "pnd_candidates": [],
    }


async def test_the_system_tool_node_emits_an_unchecked_step(recorded) -> None:
    register_all_system_tools()
    state = _state({"citations_required": True})
    updates = await execute_agent_calls_node(
        _system_call("get_datetime", state, "c-clock"), {}
    )
    assert not updates["messages"][0].content.startswith("Error:")
    (step,) = recorded
    assert step.call_id == "c-clock"
    assert step.agent_id == SYSTEM_TOOL_AGENT_DID
    assert step.tool_name == "get_datetime"
    assert step.success is True
    assert step.verdict is None  # no structural check ran on a platform tool
    assert step.output_preimage is not None  # AD-16: the raw result is hashed
    assert step.metadata["declared_acceptance"]["criteria"] == {
        "citations_required": NOT_APPLICABLE
    }
    assert "criteria_digest" in step.metadata


def test_a_platform_tool_that_reports_failure_in_its_reply_is_not_a_success() -> None:
    assert system_result_success({"error": "No active checklist"}, "{...}") is False
    assert system_result_success({"ok": False, "reason": "too big"}, "{...}") is False
    assert system_result_success("x", "Error: clock broke") is False
    assert system_result_success({"ok": True}, "{...}") is True
    assert (
        system_result_success({"error": ""}, "{...}") is True
    )  # an empty error is none
    assert system_result_success("2026-10-05", "2026-10-05") is True


async def test_a_checklist_call_with_no_checklist_is_a_failed_step(recorded) -> None:
    register_all_system_tools()
    updates = await execute_agent_calls_node(
        _system_call("update_checklist_step", _state({}), "c-no-list"), {}
    )
    assert "No active checklist" in updates["messages"][0].content
    (step,) = recorded
    assert step.success is False
    assert step.verdict is None


async def test_a_system_steps_content_is_capped_like_a_card(
    recorded, monkeypatch
) -> None:
    # the step fans out over Kafka with its content; the full result stays
    # in the hash (step_events strips args and the pre-image, never content)
    from superagent.middleware.step_events import step_result_payload
    from superagent.system_tools.registry import SYSTEM_TOOL_REGISTRY

    register_all_system_tools()
    big = {"memories": ["a private note " * 50]}
    monkeypatch.setattr(
        SYSTEM_TOOL_REGISTRY._tools["get_datetime"],
        "handler",
        AsyncMock(return_value=big),
    )
    await execute_agent_calls_node(
        _system_call("get_datetime", _state({}), "c-big"), {}
    )
    (step,) = recorded
    assert len(step.content) <= 280 < len(json.dumps(big))
    assert step.output_preimage is not None
    assert len(step_result_payload(step)["content"]) <= 280


async def test_a_system_step_is_judged_on_its_raw_result_not_the_card(
    recorded, monkeypatch
) -> None:
    # the cap is display only: an exit code past the 280-character cut is
    # still read, so the criterion fails for the real reason
    from superagent.system_tools.registry import SYSTEM_TOOL_REGISTRY

    register_all_system_tools()
    raw = {"log": "." * 400, "exit_code": 1}
    monkeypatch.setattr(
        SYSTEM_TOOL_REGISTRY._tools["get_datetime"],
        "handler",
        AsyncMock(return_value=raw),
    )
    await execute_agent_calls_node(
        _system_call("get_datetime", _state({"exit_zero": True}), "c-raw"), {}
    )
    (step,) = recorded
    assert len(step.content) <= 280
    assert step.metadata["declared_acceptance"] == {
        "result": "fail",
        "detail": "nonzero exit: 1",
        "criteria": {"exit_zero": "fail"},
    }


async def test_a_failed_system_tool_is_a_failed_step_not_a_refusal(
    recorded, monkeypatch
) -> None:
    from superagent.system_tools.registry import SYSTEM_TOOL_REGISTRY

    register_all_system_tools()
    monkeypatch.setattr(
        SYSTEM_TOOL_REGISTRY._tools["get_datetime"],
        "handler",
        AsyncMock(side_effect=RuntimeError("clock broke")),
    )
    await execute_agent_calls_node(
        _system_call("get_datetime", _state({}), "c-broke"), {}
    )
    (step,) = recorded
    assert step.success is False
    assert step.verdict is None
    assert "declared_acceptance" not in step.metadata  # nothing declared
    # the hash commits to the error text itself, not to the observer's
    # display-content fallback (which is taken only when no pre-image exists)
    assert step.content == "Error: clock broke"
    assert step.output_preimage == step.content


async def test_a_counts_match_firing_carries_its_operands_on_system_steps(
    recorded,
) -> None:
    register_all_system_tools()
    operands = {"left": "did:orcha:agent:a#n", "right": "did:orcha:agent:b#n"}
    state = _state(
        {"counts_match": True},
        routine_context={"criteria_operands": {"counts_match": operands}},
    )
    await execute_agent_calls_node(_system_call("get_datetime", state, "c-r"), {})
    (step,) = recorded
    assert step.metadata["run_criteria"] == {"counts_match": operands}
    # judged at seal, never per step: no "unsupported criterion: counts_match"
    assert "declared_acceptance" not in step.metadata


# ---------------------------------------------------------------------------
# Sealed, verified and gated
# ---------------------------------------------------------------------------


@pytest.fixture()
def sealed(monkeypatch):
    """The real attestation observer and gate, on a fake database."""
    from validator.run_observer import RunAttestationObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    db = DB()
    attestation = RunAttestationObserver(db=db)
    set_observer(CompositeObserver([attestation, SettlementGateObserver(attestation)]))
    real = settle_gate.gate_verdict_only

    async def on_this_db(**kwargs: Any) -> Any:
        return await real(db=db, **kwargs)

    monkeypatch.setattr(settle_gate, "gate_verdict_only", on_this_db)
    register_all_system_tools()

    async def seal() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from superagent.middleware.observers import get_observer

        await get_observer().on_run_complete(SESSION)
        (envelope,) = attestation.envelopes.values()
        return envelope, list(db.attestedsettlement.rows)

    seal.db = db  # the fake database, for exports read after sealing
    yield seal
    set_observer(NoOpObserver())


def _verdict(envelope: dict[str, Any], check: str) -> dict[str, Any] | None:
    return next((v for v in envelope["verdicts"] if v["check"] == check), None)


async def test_a_cited_turn_that_touched_a_checklist_settles(sealed) -> None:
    state = _state({"citations_required": True})
    await execute_agent_calls_node(_system_call("get_datetime", state, "c-clock"), {})
    await _agent_step(state, CITED, call_id="c-rag")
    envelope, ledger = await sealed()

    assert verify_run_attestation(envelope).valid
    assert [s["tool"] for s in envelope["steps"]] == [
        "get_datetime",
        "did:orcha:agent:test-runner#run_tests",
    ]
    assert SYSTEM_TOOL_AGENT_DID in envelope["agent_dids"]
    assert _verdict(envelope, "declared_acceptance") == {
        "check": "declared_acceptance",
        "result": "pass",
        "detail": "ok",
    }
    # one structural verdict: the agent step's; the system step is unchecked
    structural = [
        v for v in envelope["verdicts"] if v["check"] == "structural_verification"
    ]
    assert len(structural) == 1
    (row,) = ledger
    assert row["outcome"] == "settled"
    assert row["call_id"] is None


async def test_exit_zero_with_nothing_that_reports_an_exit_code_is_refused(
    sealed,
) -> None:
    state = _state({"exit_zero": True})
    await execute_agent_calls_node(_system_call("get_datetime", state, "c-clock"), {})
    envelope, ledger = await sealed()

    assert verify_run_attestation(envelope).valid
    assert _verdict(envelope, "declared_acceptance") == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "no step reported an exit code",
    }
    (row,) = ledger
    assert row["outcome"] == "refused"
    assert firing_view.gate_checks(row) == [settle_gate.CHECK_VERDICT_FAIL]


async def test_a_failing_suite_past_the_card_is_refused_for_its_exit_code(
    sealed,
) -> None:
    state = _state({"exit_zero": True})
    await _agent_step(state, json.dumps(_runner_output(1)), call_id="c-suite")
    envelope, ledger = await sealed()

    step = envelope["steps"][0]
    assert {"args_hash", "output_hash", "success"} <= set(step)
    assert _verdict(envelope, "declared_acceptance") == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "exit_zero: nonzero exit: 1",
    }
    (row,) = ledger
    assert row["outcome"] == "refused"
