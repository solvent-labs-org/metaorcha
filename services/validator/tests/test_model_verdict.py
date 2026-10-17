"""Story 3.3 / AD-21: the model that ran the turn is a signed verdict.

The pipeline puts ``model`` ("<route>/<model id>") on each step's metadata;
the observer signs ``{check: "model", result: "pass", detail}`` once per
distinct model inside the digest. The published verifier accepts it unchanged
(no new field), a ``pass`` never refuses, and the entry sits outside the
verifier's check order.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeDB, _step_result
from emerge.run_attestation import verify_run_attestation
from validator.run_observer import RunAttestationObserver

AGENT = "did:orcha:agent:rag"
MODEL_A = "openrouter/anthropic/claude-haiku-4.5-20251001"
MODEL_B = "local/qwen2.5:3b"


async def _seal(*records) -> dict[str, Any]:
    observer = RunAttestationObserver(db=FakeDB())
    for record in records:
        await observer.on_step_complete(record)
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


def _call(call_id: str, model: Any = MODEL_A) -> Any:
    metadata = {} if model is None else {"model": model}
    return _step_result(
        call_id,
        agent_id=AGENT,
        tool_name="rag__search",
        content="found it",
        metadata=metadata,
    )


def _models(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    return [v for v in envelope["verdicts"] if v["check"] == "model"]


@pytest.mark.asyncio
async def test_the_turn_signs_its_model_once() -> None:
    envelope = await _seal(_call("c1"), _call("c2"), _call("c3"))
    assert verify_run_attestation(envelope).valid
    assert _models(envelope) == [
        {"check": "model", "result": "pass", "detail": MODEL_A}
    ]


@pytest.mark.asyncio
async def test_two_models_in_one_turn_sign_in_first_use_order() -> None:
    envelope = await _seal(
        _call("c1", MODEL_B), _call("c2", MODEL_A), _call("c3", MODEL_B)
    )
    assert verify_run_attestation(envelope).valid
    assert [v["detail"] for v in _models(envelope)] == [MODEL_B, MODEL_A]


@pytest.mark.asyncio
async def test_the_model_is_inside_the_signed_bytes() -> None:
    envelope = await _seal(_call("c1"))
    assert verify_run_attestation(envelope).valid
    for entry in envelope["verdicts"]:
        if entry["check"] == "model":
            entry["detail"] = MODEL_B
    assert not verify_run_attestation(envelope).valid


@pytest.mark.asyncio
async def test_a_value_that_cannot_be_signed_is_left_out_not_fatal() -> None:
    envelope = await _seal(
        _call("c1", None),
        _call("c2", ""),
        _call("c3", "local/qwén"),
        _call("c4", "x" * 201),
        _call("c5", 42),
    )
    assert verify_run_attestation(envelope).valid
    assert _models(envelope) == []
    assert len(envelope["steps"]) == 5


@pytest.mark.asyncio
async def test_the_bound_is_two_hundred_printable_characters() -> None:
    # the producer caps at 200 (superagent.turn_model._MAX_LEN); the bound
    # here must admit exactly what it emits
    exact = "local/" + "m" * 194
    assert len(exact) == 200
    envelope = await _seal(_call("c1", exact), _call("c2", exact + "x"))
    assert verify_run_attestation(envelope).valid
    assert [v["detail"] for v in _models(envelope)] == [exact]


def test_the_settle_gate_is_unmoved_by_the_model() -> None:
    from superagent.pricing.settle_gate import (
        _VERIFIER_CHECK_ORDER,
        _verdicts_policy_refuse,
    )

    envelope = {"verdicts": [{"check": "model", "result": "pass", "detail": MODEL_A}]}
    assert _verdicts_policy_refuse(envelope) == []
    assert "model" not in _VERIFIER_CHECK_ORDER
