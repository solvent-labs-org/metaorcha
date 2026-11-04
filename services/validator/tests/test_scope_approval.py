"""Story 1.5 / AD-21: who approved is a signed verdict in the receipt.

The pipeline puts ``scope_approval`` on the step's metadata; the observer
turns it into ``{check: "scope_approval:<call_id>", result, detail}`` inside
the signed digest. The published verifier accepts it unchanged (no new
field), a decline is ``warn`` and never ``fail``, and the entry sits outside
the verifier's check order.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeDB, _step_result
from emerge.run_attestation import verify_run_attestation
from validator.run_observer import RunAttestationObserver

CONNECTION = "did:orcha:agent:gh-connection"
APPROVER = "did:orcha:user:9f1c2d3e"


async def _seal(*records) -> dict[str, Any]:
    observer = RunAttestationObserver(db=FakeDB())
    for record in records:
        await observer.on_step_complete(record)
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


def _call(call_id: str, capability: str, *, success: bool = True, metadata=None):
    record = _step_result(
        call_id,
        agent_id=CONNECTION,
        tool_name=f"gh__{capability}",
        success=success,
        content="issue #12 created" if success else "Error: scope_declined: no",
        metadata=metadata or {},
    )
    record.capability_id = capability
    return record


@pytest.mark.asyncio
async def test_an_approved_write_names_the_approver() -> None:
    envelope = await _seal(
        _call("c1", "list_issues"),
        _call(
            "c2",
            "create_issue",
            metadata={"scope_approval": {"result": "pass", "detail": APPROVER}},
        ),
    )
    verdict = verify_run_attestation(envelope)
    assert verdict.valid, verdict.checks
    assert {"check": "scope_approval:c2", "result": "pass", "detail": APPROVER} in (
        envelope["verdicts"]
    )
    # exactly one approval entry, for the approved call only
    approvals = [v for v in envelope["verdicts"] if v["check"].startswith("scope_")]
    assert approvals == [
        {"check": "scope_approval:c2", "result": "pass", "detail": APPROVER}
    ]
    assert envelope["steps"][1]["tool"] == f"{CONNECTION}#create_issue"


@pytest.mark.asyncio
async def test_a_declined_write_is_warn_never_fail() -> None:
    envelope = await _seal(
        _call(
            "c3",
            "delete_repo",
            success=False,
            metadata={"scope_approval": {"result": "warn", "detail": "declined"}},
        ),
    )
    assert verify_run_attestation(envelope).valid
    assert {"check": "scope_approval:c3", "result": "warn", "detail": "declined"} in (
        envelope["verdicts"]
    )
    results = {v["result"] for v in envelope["verdicts"] if "scope_" in v["check"]}
    assert results == {"warn"}


@pytest.mark.asyncio
async def test_the_approver_is_inside_the_signed_bytes() -> None:
    envelope = await _seal(
        _call(
            "c2",
            "create_issue",
            metadata={"scope_approval": {"result": "pass", "detail": APPROVER}},
        ),
    )
    assert verify_run_attestation(envelope).valid
    for entry in envelope["verdicts"]:
        if entry["check"] == "scope_approval:c2":
            entry["detail"] = "did:orcha:user:someone-else"
    assert not verify_run_attestation(envelope).valid


@pytest.mark.asyncio
async def test_a_malformed_approval_adds_no_verdict() -> None:
    envelope = await _seal(
        _call("c4", "create_issue", metadata={"scope_approval": {"result": "fail"}}),
        _call("c5", "create_issue", metadata={"scope_approval": "yes"}),
    )
    assert verify_run_attestation(envelope).valid
    assert not [v for v in envelope["verdicts"] if v["check"].startswith("scope_")]


def test_the_settle_gate_does_not_refuse_a_decline() -> None:
    from superagent.pricing.settle_gate import (
        _VERIFIER_CHECK_ORDER,
        _verdicts_policy_refuse,
    )

    envelope = {
        "verdicts": [
            {"check": "scope_approval:c2", "result": "pass", "detail": APPROVER},
            {"check": "scope_approval:c3", "result": "warn", "detail": "declined"},
        ]
    }
    assert _verdicts_policy_refuse(envelope) == []
    assert not any(c.startswith("scope_approval") for c in _VERIFIER_CHECK_ORDER)
