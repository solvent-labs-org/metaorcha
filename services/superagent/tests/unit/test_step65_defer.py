"""Story 2.7 (AD-12): pipeline step 6.5 stores a charge before it returns.

With ``SETTLEMENT_REQUIRE_ATTESTATION`` on, a charged call's deferred settle
is written into the map by step 6.5 itself — not inside a background task —
so when the run seals the gate already knows the run charged something and
never judges it "verdict only" first. Only the reserve release is left to a
task. With the flag off, step 6.5 schedules ``settle_invocation`` exactly as
before and defers nothing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from superagent.config import settings
from superagent.middleware import pipeline as pipeline_mod
from superagent.middleware.observers import NoOpObserver, set_observer
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.pricing import settlement

DID = "did:orcha:agent:priced-1a2b3c4d"
FEE = Decimal("0.25")


@pytest.fixture(autouse=True)
def _clean():
    settlement._deferred_settles.clear()
    set_observer(NoOpObserver())
    yield
    settlement._deferred_settles.clear()


@pytest.fixture()
def spies(monkeypatch):
    """Record what step 6.5 hands to the background, without running it.

    Each spy records at call time — when the coroutine is created, before
    anything is scheduled — so a test can tell what step 6.5 did itself
    from what it left to a task.
    """
    seen: dict[str, list[Any]] = {"settle": [], "release": []}

    def _settle(**kwargs):
        seen["settle"].append(kwargs)
        return asyncio.sleep(0)

    def _release(session_id, call_id):
        seen["release"].append((session_id, call_id))
        return asyncio.sleep(0)

    monkeypatch.setattr(settlement, "settle_invocation", _settle)
    monkeypatch.setattr(settlement, "release_reserve", _release)
    return seen


async def _charged_call() -> None:
    mw = ExecutionMiddleware(state={"session_id": "s1", "user_id": "u1"})
    manifest = {"agent_id": DID, "name": "Priced", "tags": ["a2a"], "is_active": True}
    with (
        patch.object(mw, "_get_capability_schema", AsyncMock(return_value=None)),
        patch.object(mw, "_resolve_base_fee", AsyncMock(return_value=FEE)),
        patch("superagent.pricing.guard.payment_guard", AsyncMock()),
        patch.object(
            pipeline_mod.PreFlightManager,
            "run",
            AsyncMock(
                return_value={"headers": {}, "manifest": manifest, "resolved_env": None}
            ),
        ),
        patch.object(
            mw,
            "_dispatch",
            AsyncMock(return_value="done"),
        ),
        patch(
            "superagent.middleware.pipeline.OutputNormalizer.normalize",
            AsyncMock(return_value={"content": "done"}),
        ),
    ):
        await mw.execute(
            agent_id=DID,
            capability_id="quote",
            protocol="A2A",
            tool_name="t__quote",
            args={},
            call_id="call_1",
        )


async def test_flag_on_the_charge_is_stored_before_step_65_returns(
    monkeypatch, spies
) -> None:
    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    await _charged_call()

    stored = settlement._deferred_settles["s1"]
    assert (stored["call_id"], stored["base_fee"], stored["user_id"]) == (
        "call_1",
        FEE,
        "u1",
    )
    assert spies["settle"] == []  # no background settle to race the seal
    assert spies["release"] == [("s1", "call_1")]


async def test_flag_off_schedules_the_settle_as_before(monkeypatch, spies) -> None:
    monkeypatch.setattr(settings, "settlement_require_attestation", False)
    await _charged_call()

    assert settlement._deferred_settles == {}
    assert spies["release"] == []
    (call,) = spies["settle"]
    assert call == {
        "user_id": "u1",
        "agent_id": DID,
        "session_id": "s1",
        "call_id": "call_1",
        "base_fee": FEE,
        "latency_ms": call["latency_ms"],
        "execution_success": True,
        "platform_tokens": 0,
    }


@pytest.mark.parametrize(
    ("flag", "success", "deferred"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_defer_charge_stores_only_a_gated_successful_call(
    monkeypatch, flag, success, deferred
) -> None:
    """A failed call keeps the stock ERROR path; with the flag off nothing waits."""
    monkeypatch.setattr(settings, "settlement_require_attestation", flag)
    got = settlement.defer_charge(
        user_id="u1",
        agent_id=DID,
        session_id="s1",
        call_id="call_1",
        base_fee=FEE,
        latency_ms=5,
        execution_success=success,
    )
    assert got is deferred
    assert ("s1" in settlement._deferred_settles) is deferred
