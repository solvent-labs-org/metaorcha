"""Story 2.3: a firing is attested, and a failed check refuses it.

End to end, with only the database and the connection's reply faked: a
routine fires through the scheduler; its turn goes through the real
pipeline, which evaluates the routine's declared criteria on the step; the
real run-attestation observer seals and signs the envelope; the real gate
observer judges the run (verdict only — an MCP connection is never charged,
AD-12); the scheduler then writes the gate's judgement onto the firing row.

- a criterion that passes → a ``pass`` verdict, the criteria digest in
  ``policy_version``, the run ``settled``, and so the firing;
- a criterion that fails → a ``fail`` verdict; the gate refuses with
  ``verdict_fail`` (``call_id`` NULL, no credit) and the firing is
  ``refused`` — while the published verifier still reports the envelope
  valid, because the refusal is honest and inside the signed bytes;
- the same run re-entering the gate claims nothing twice.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from emerge.run_attestation import verify_run_attestation
from superagent.config import settings
from superagent.graph.runner import _done_event
from superagent.middleware import pipeline as pipeline_mod
from superagent.middleware.criteria import criteria_digest
from superagent.middleware.observers import (
    CompositeObserver,
    NoOpObserver,
    emit_run_complete,
    set_observer,
)
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.pricing import settle_gate
from superagent.pricing.settle_gate import (
    CHECK_ALREADY_SETTLED,
    CHECK_VERDICT_FAIL,
    SettlementGateObserver,
)
from superagent.workflow import firing_rules as rules

from .test_routine_scheduler import DB, DID, _scheduler, _seed, _tick

CRITERIA = {"citations_required": True}
CITED = json.dumps(
    {
        "summary": "3 open issues",
        "citations": [{"chunk_id": "c1", "source_title": "#12", "excerpt": "..."}],
    }
)
UNCITED = "3 open issues"


class PipelineRunner:
    """``SessionRunner.run_turn`` reduced to one step: the routine's
    connection is called through the real pipeline, the run boundary is
    dispatched through the installed observers, and ``done`` carries the
    sealed run_id exactly as the runner builds it."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def run_turn(self, **kwargs: Any):
        self.calls.append(kwargs)
        session_id = kwargs["session_id"]
        mw = ExecutionMiddleware(
            state={
                "session_id": session_id,
                "user_id": kwargs["user_id"],
                "routine_context": kwargs["routine_context"],
                "_declared_criteria": kwargs["acceptance_criteria"],
            }
        )
        manifest = {
            "agent_id": DID,
            "name": "GitHub",
            "tags": ["mcp", "user", "connection"],
            "is_active": True,
            "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
            "security": {"auth_strategies": []},
            "capabilities": [],
        }
        with (
            patch.object(mw, "_get_capability_schema", AsyncMock(return_value=None)),
            patch.object(
                pipeline_mod.PreFlightManager,
                "run",
                AsyncMock(
                    return_value={
                        "headers": {},
                        "manifest": manifest,
                        "resolved_env": None,
                    }
                ),
            ),
            patch.object(mw, "_dispatch", AsyncMock(return_value=self.reply)),
        ):
            await mw.execute(
                agent_id=DID,
                capability_id="list_issues",
                protocol="MCP",
                tool_name="gh__list_issues",
                args={"owner": "o", "repo": "r"},
                call_id="call_1",
            )
        await emit_run_complete(session_id)
        yield _done_event(session_id)


@pytest.fixture()
def world(monkeypatch):
    """The scheduler's DB doubles as the attestation store and the ledger."""
    from validator.run_observer import RunAttestationObserver

    monkeypatch.setattr(settings, "connections_enabled", True)
    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    db = DB()
    _seed(db, criteria=CRITERIA)

    attestation = RunAttestationObserver(db=db)
    set_observer(CompositeObserver([attestation, SettlementGateObserver(attestation)]))
    real = settle_gate.gate_verdict_only

    async def on_this_db(**kwargs: Any) -> Any:
        return await real(db=db, **kwargs)

    monkeypatch.setattr(settle_gate, "gate_verdict_only", on_this_db)
    yield db
    set_observer(NoOpObserver())


async def _fire(db: DB, reply: str) -> tuple[dict[str, Any], dict[str, Any]]:
    await _tick(_scheduler(db, PipelineRunner(reply)))
    (firing,) = db.routinefiring.rows
    (stored,) = db.attestation.rows
    envelope = getattr(stored["payload"], "data", stored["payload"])
    return firing, json.loads(json.dumps(envelope))


def _ledger(db: DB) -> list[tuple[Any, ...]]:
    return [
        (
            r["outcome"],
            r["settled_run_id"],
            r["call_id"],
            list(getattr(r["failed_checks"], "data", r["failed_checks"])),
        )
        for r in db.attestedsettlement.rows
    ]


def _declared(envelope: dict[str, Any]) -> dict[str, Any]:
    (entry,) = [v for v in envelope["verdicts"] if v["check"] == "declared_acceptance"]
    return entry


async def test_a_firing_whose_criterion_passes_is_settled(world) -> None:
    firing, envelope = await _fire(world, CITED)

    assert _declared(envelope)["result"] == "pass"
    assert criteria_digest(CRITERIA) in envelope["policy_version"]
    assert verify_run_attestation(envelope).valid is True

    run_id = envelope["run_id"]
    assert _ledger(world) == [("settled", run_id, None, [])]
    assert (firing["state"], firing["detail"], firing["run_id"]) == (
        rules.SETTLED,
        None,
        run_id,
    )


async def test_a_firing_whose_criterion_fails_is_refused_and_still_verifies(
    world,
) -> None:
    firing, envelope = await _fire(world, UNCITED)

    assert _declared(envelope)["result"] == "fail"
    assert criteria_digest(CRITERIA) in envelope["policy_version"]
    # The refusal is the gate's policy, not a broken receipt.
    assert verify_run_attestation(envelope).valid is True

    assert _ledger(world) == [("refused", None, None, [CHECK_VERDICT_FAIL])]
    assert (firing["state"], firing["detail"], firing["run_id"]) == (
        rules.REFUSED,
        CHECK_VERDICT_FAIL,
        envelope["run_id"],
    )


async def test_a_firing_moves_no_money(world) -> None:
    await _fire(world, CITED)
    # The scheduler's DB has no users or transactions table: a credit write
    # would have raised, and the gate would have refused instead of settling.
    assert not hasattr(world, "transaction") and not hasattr(world, "user")
    assert [row[0] for row in _ledger(world)] == ["settled"]


async def test_the_same_run_re_entering_the_gate_claims_nothing_twice(world) -> None:
    firing, envelope = await _fire(world, CITED)

    again = await settle_gate.gate_verdict_only(run_id=envelope["run_id"])

    assert again["failed_checks"] == [CHECK_ALREADY_SETTLED]
    assert [row[:2] for row in _ledger(world)] == [
        ("settled", envelope["run_id"]),
        ("refused", None),
    ]
    assert firing["state"] == rules.SETTLED  # the row keeps the run's judgement
