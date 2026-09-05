"""Read-back canonicalization proof (Story 1.3, AR-17).

Postgres JSONB may reorder keys; verification must not depend on key order.
These tests prove the property end-to-end: persist → read back →
deterministically scramble every dict's key order → verify still green.
The scramble recurses through lists to reach nested dicts but NEVER reorders
list elements (steps/verdicts order is semantically significant).

No production change is anticipated — both verifiers hash
``canonical_json_bytes`` (sorted keys) by construction. If these tests fail,
that is a real bug; surface it, don't paper over it.

Scope note: real JSONB also normalizes numbers and drops whitespace — both
irrelevant here because envelope numeric fields are ints and a
``json.loads(json.dumps(...))`` round-trip (applied below before scrambling)
preserves them. The scramble twin lives in sdk/tests/test_verify_cli.py —
keep the two in sync.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeDB
from emerge_node.envelope import canonical_json_bytes
from validator.run_envelope import (
    build_run_envelope,
    get_run_attestation_by_run_id,
    persist_run_attestation,
    sign_run_envelope,
    verify_run_envelope,
)

EXAMPLE_STEPS_RAW = [
    {
        "call_id": "call-7f3a",
        "tool": "search_docs",
        "args": {"query": "refund policy"},
        "output": {"documents": ["policy-v3"], "count": 1},
        "success": True,
        "latency_ms": 132,
    },
    {
        "call_id": "call-7f3b",
        "tool": "summarize",
        "args": {"text": "policy-v3 contents", "max_words": 50},
        "output": {"summary": "Refunds within 30 days."},
        "success": True,
        "latency_ms": 481,
    },
]


def scramble_keys(value: Any) -> Any:
    """Deterministically reverse every dict's key order, recursing through
    lists WITHOUT reordering list elements (JSONB-reorder simulation)."""
    if isinstance(value, dict):
        return {key: scramble_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [scramble_keys(item) for item in value]
    return value


def _signed(run_id: str) -> dict[str, Any]:
    return sign_run_envelope(
        build_run_envelope(
            run_id=run_id,
            agent_dids=["did:orcha:agent:rulebook-rag"],
            charter_hash=None,
            policy_version="p/1",
            steps=[dict(step) for step in EXAMPLE_STEPS_RAW],
            verdicts=[{"check": "structural_verification", "result": "pass"}],
            started_at="2026-08-06T01:00:00Z",
            finished_at="2026-08-06T01:00:01Z",
            signer_did="did:orcha:system:validator",
        )
    )


def test_scramble_actually_mutates_key_order() -> None:
    scrambled = scramble_keys({"a": 1, "b": 2, "c": 3})
    assert list(scrambled) == ["c", "b", "a"]
    # Lists are traversed, never reordered.
    assert scramble_keys({"s": [{"x": 1, "y": 2}, {"z": 3}]}) == {
        "s": [{"y": 2, "x": 1}, {"z": 3}]
    }


@pytest.mark.asyncio
async def test_readback_verifies_despite_key_reorder() -> None:
    db = FakeDB()
    envelope = _signed("run-readback01")
    await persist_run_attestation("sess-1", envelope, db=db)

    read_back = await get_run_attestation_by_run_id("run-readback01", db=db)
    assert read_back is not None
    # Simulate the JSONB round-trip (FakeDB stores the object, never serializes).
    read_back = json.loads(json.dumps(read_back))

    scrambled = scramble_keys(read_back)
    assert list(scrambled) != list(read_back)  # the scramble is a real mutation

    # AR-17: order-independence invariant both verifiers rely on.
    assert canonical_json_bytes(scrambled) == canonical_json_bytes(read_back)
    # Read-back bytes verify offline, key order notwithstanding.
    assert verify_run_envelope(scrambled) is True


@pytest.mark.asyncio
async def test_readback_tampered_still_refuses() -> None:
    """Negative control: reorder is tolerated, tamper is not."""
    db = FakeDB()
    envelope = _signed("run-readback02")
    await persist_run_attestation("sess-1", envelope, db=db)

    read_back = await get_run_attestation_by_run_id("run-readback02", db=db)
    assert read_back is not None
    read_back = json.loads(json.dumps(read_back))

    tampered = scramble_keys(read_back)
    tampered["steps"][1]["output_hash"] = "0" * 64  # break the chain
    assert verify_run_envelope(tampered) is False
