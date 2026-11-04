"""Story 3.2: the session export states what its signed receipt covers (FR-8).

The statement is the SDK verifier's (``coverage_statement``), computed from
the sealed envelope when the export is built — never stored — and present in
every export: a chat session, a firing session, a session nothing sealed,
and a deployment without the SDK. A stored envelope is called a signed
receipt only after it verified offline when the export was built; one that
does not verify is named as such and nothing is read from it.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest
from emerge.run_attestation import COVERAGE_STATEMENT
from superagent.api import audit
from superagent.api.audit import build_run_audit, load_settlement
from superagent.middleware.observers import StepResult

from .test_routine_scheduler import DB

SESSION = "sess-cov"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _step(call_id: str, agent: str, tool: str) -> StepResult:
    return StepResult(
        call_id=call_id,
        agent_id=agent,
        capability_id=tool,
        protocol="A2A" if agent.startswith("did:orcha:agent") else "SYSTEM",
        tool_name=tool,
        success=True,
        content="ok",
        session_id=SESSION,
        user_id="u1",
        verdict={"verified": True, "reason": "ok"},
    )


async def _sealed_db() -> tuple[DB, dict[str, Any]]:
    from validator.run_observer import RunAttestationObserver

    db = DB()
    observer = RunAttestationObserver(db=db)
    await observer.on_step_complete(
        _step("c1", "did:orcha:system:tools", "get_datetime")
    )
    await observer.on_step_complete(_step("c2", "did:orcha:agent:rag", "search"))
    await observer.on_run_complete(SESSION)
    (envelope,) = observer.envelopes.values()
    return db, envelope


async def test_a_sealed_chat_session_states_its_receipt_coverage() -> None:
    db, envelope = await _sealed_db()
    evidence = await load_settlement(SESSION, db=db)
    body = build_run_audit(SESSION, [], evidence=evidence).model_dump(exclude_none=True)

    coverage = body["coverage"]
    assert coverage["statement"] == COVERAGE_STATEMENT
    assert coverage["run_id"] == envelope["run_id"]
    assert coverage["receipt_steps"] == 2
    assert coverage["receipt_tools"] == [
        "get_datetime",
        "did:orcha:agent:rag#search",
    ]
    assert coverage["export"].startswith(
        f"The signed receipt for run {envelope['run_id']} has 2 step(s)."
    )
    assert "not itself signed" in coverage["export"]
    assert "settlement" not in body  # a chat session: coverage, never settlement


async def test_a_stored_envelope_that_does_not_verify_is_not_called_signed() -> None:
    db, envelope = await _sealed_db()
    evidence = await load_settlement(SESSION, db=db)
    assert evidence.envelope is not None
    forged = dict(evidence.envelope)
    forged["steps"] = [*forged["steps"], {**forged["steps"][-1], "tool": "x#forged"}]
    forged["verdicts"] = [
        {"check": "model", "result": "pass", "detail": "local/forged"}
    ]
    tampered = audit.SettlementEvidence(run_id=evidence.run_id, envelope=forged)

    body = build_run_audit(SESSION, [], evidence=tampered).model_dump(exclude_none=True)
    coverage = body["coverage"]
    assert coverage["statement"] == COVERAGE_STATEMENT
    assert coverage["run_id"] == envelope["run_id"]
    assert coverage["export"].startswith(
        f"A stored receipt for run {envelope['run_id']} did not verify"
    )
    assert "receipt_steps" not in coverage
    assert coverage["receipt_tools"] == []


def test_receipt_verifies_reads_true_false_and_none(monkeypatch) -> None:
    import json
    from pathlib import Path

    golden = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "docs/spec/test-vectors/run-attestation-golden.json"
        ).read_text()
    )
    valid = golden["valid"]
    assert audit.receipt_verifies(valid) is True
    assert audit.receipt_verifies(golden["tampered"]) is False
    assert audit.receipt_verifies({**valid, "steps_root": "0" * 64}) is False
    assert audit.receipt_verifies(None) is False
    assert audit.receipt_verifies("junk") is False
    monkeypatch.setitem(sys.modules, "emerge.run_attestation", None)
    assert audit.receipt_verifies(valid) is None


@pytest.mark.parametrize("evidence", [None, audit.SettlementEvidence()])
def test_an_export_with_no_receipt_still_states_its_coverage(evidence) -> None:
    body = build_run_audit(SESSION, [], evidence=evidence).model_dump(exclude_none=True)
    coverage = body["coverage"]
    assert coverage["statement"] == COVERAGE_STATEMENT
    assert "run_id" not in coverage
    assert coverage["export"].startswith(audit._NO_RECEIPT_READ)


def test_an_unreadable_envelope_is_not_counted() -> None:
    evidence = audit.SettlementEvidence(run_id="r1", envelope=None)
    coverage = build_run_audit(SESSION, [], evidence=evidence).coverage
    assert coverage.receipt_steps is None
    assert coverage.export.startswith(audit._NO_RECEIPT_READ)


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        {},
        {"steps": "x"},
        {"steps": [{"tool": "b"}, {"tool": "a#c"}, {"tool": "b"}, "junk", {}]},
    ],
)
def test_the_fallback_reads_as_the_sdk_does(envelope) -> None:
    from emerge.run_attestation import coverage_statement

    assert audit._COVERAGE_FALLBACK == COVERAGE_STATEMENT
    assert audit._coverage_without_sdk(envelope) == coverage_statement(envelope)


def test_without_the_sdk_the_export_still_states_its_coverage(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "emerge.run_attestation", None)
    with pytest.raises(ImportError):
        importlib.import_module("emerge.run_attestation")
    coverage = audit.coverage("r1", {"steps": [{"tool": "x"}]}, verified=None)
    assert coverage.statement == audit._COVERAGE_FALLBACK
    assert coverage.receipt_steps == 1  # read, so never "no receipt was read"
    assert coverage.receipt_tools == ["x"]
    # read as stored, and said so: the verifier was not there to check it
    assert coverage.export.startswith("The stored receipt for run r1 has 1 step(s)")
    assert "could not check its signature" in coverage.export
    assert audit.coverage(None, None).export.startswith(audit._NO_RECEIPT_READ)


async def test_the_mailed_summary_carries_the_coverage() -> None:
    from superagent.system_tools.mailer import _render_receipt

    db, _ = await _sealed_db()
    evidence = await load_settlement(SESSION, db=db)
    body = _render_receipt(build_run_audit(SESSION, [], evidence=evidence))
    assert COVERAGE_STATEMENT in _flat(body)
