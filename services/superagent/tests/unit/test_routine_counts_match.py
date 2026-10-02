"""Story 2.4: ``counts_match`` — two sources disagree, and the run is refused.

The pipeline stamps the routine's ``counts_match`` operands on every step it
records (successful, failed and refused alike); the run observer reads each
source's last successful call at seal and signs one ``counts_match`` verdict
naming both counts and both sources; the gate refuses a ``fail`` like any
other (``verdict_fail``), and the firing ends ``refused``. The verifier still
reports the envelope valid: the disagreement is inside the signed bytes.

End to end as story 2.3's test: only the database and the connections'
replies are faked.
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
from superagent.pricing.settle_gate import CHECK_VERDICT_FAIL, SettlementGateObserver
from superagent.workflow import firing_rules as rules

from .test_routine_scheduler import DB, DID, _scheduler, _seed, _tick

NOTION = "did:orcha:agent:notion-5e6f7a8b"
LEFT = f"{DID}#list_issues"
RIGHT = f"{NOTION}#query_database"
OPERANDS = {
    "left": LEFT,
    "left_path": "/total_count",
    "right": RIGHT,
    "right_path": "/results",
    "key": "open_issues",
}
CRITERIA = {"counts_match": True}
SOURCES = (
    f"left_source={LEFT} left_path=/total_count "
    f"right_source={RIGHT} right_path=/results"
)


def _issues(n: int) -> str:
    return json.dumps({"total_count": n})


def _rows(n: int) -> str:
    return json.dumps({"results": [{"id": i} for i in range(n)]})


def _manifest(did: str) -> dict[str, Any]:
    return {
        "agent_id": did,
        "name": did.rsplit(":", 1)[-1],
        "tags": ["mcp", "user", "connection"],
        "is_active": True,
        "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        "security": {"auth_strategies": []},
        "capabilities": [],
    }


Call = tuple[str, str, str, Any]  # (agent_id, capability_id, call_id, reply)


async def _execute(mw: ExecutionMiddleware, calls: list[Call]) -> None:
    """Each call through the real pipeline; an Exception reply fails dispatch."""
    for agent_id, capability, call_id, reply in calls:
        dispatch = (
            AsyncMock(side_effect=reply)
            if isinstance(reply, Exception)
            else AsyncMock(return_value=reply)
        )
        with (
            patch.object(mw, "_get_capability_schema", AsyncMock(return_value=None)),
            patch.object(
                pipeline_mod.PreFlightManager,
                "run",
                AsyncMock(
                    return_value={
                        "headers": {},
                        "manifest": _manifest(agent_id),
                        "resolved_env": None,
                    }
                ),
            ),
            patch.object(mw, "_dispatch", dispatch),
        ):
            try:
                await mw.execute(
                    agent_id=agent_id,
                    capability_id=capability,
                    protocol="MCP",
                    tool_name=f"x__{capability}",
                    args={"q": call_id},
                    call_id=call_id,
                )
            except Exception:
                if not isinstance(reply, Exception):
                    raise


class ScriptedRunner:
    """``SessionRunner.run_turn`` reduced to a scripted list of calls."""

    def __init__(self, calls: list[Call]) -> None:
        self.script = calls

    async def run_turn(self, **kwargs: Any):
        session_id = kwargs["session_id"]
        mw = ExecutionMiddleware(
            state={
                "session_id": session_id,
                "user_id": kwargs["user_id"],
                "routine_context": kwargs["routine_context"],
                "_declared_criteria": kwargs["acceptance_criteria"],
            }
        )
        await _execute(mw, self.script)
        await emit_run_complete(session_id)
        yield _done_event(session_id)


@pytest.fixture()
def world(monkeypatch):
    """Builds a scheduler DB that doubles as attestation store and ledger."""
    from validator.run_observer import RunAttestationObserver

    monkeypatch.setattr(settings, "connections_enabled", True)
    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    real = settle_gate.gate_verdict_only

    def build(
        criteria: dict[str, Any] = CRITERIA, operands: dict[str, Any] | None = None
    ) -> DB:
        db = DB()
        _seed(
            db,
            agents_used=[DID, NOTION],
            parameters={"scope_allow": [], "model": "m/1"},
            criteria=criteria,
            criteria_operands={"counts_match": operands or OPERANDS},
        )
        db.agent.rows.append(
            {"id": NOTION, "user_id": "bob", "office_id": "off-1", "is_active": True}
        )
        attestation = RunAttestationObserver(db=db)
        set_observer(
            CompositeObserver([attestation, SettlementGateObserver(attestation)])
        )

        async def on_this_db(**kwargs: Any) -> Any:
            return await real(db=db, **kwargs)

        monkeypatch.setattr(settle_gate, "gate_verdict_only", on_this_db)
        return db

    yield build
    set_observer(NoOpObserver())


async def _fire(db: DB, calls: list[Call]) -> tuple[dict[str, Any], dict[str, Any]]:
    await _tick(_scheduler(db, ScriptedRunner(calls)))
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


def _counts(envelope: dict[str, Any]) -> dict[str, Any]:
    (entry,) = [v for v in envelope["verdicts"] if v["check"] == "counts_match"]
    return entry


async def test_equal_counts_settle_the_firing(world) -> None:
    db = world()
    firing, envelope = await _fire(
        db,
        [
            (DID, "list_issues", "call_1", _issues(11)),
            (NOTION, "query_database", "call_2", _rows(11)),
        ],
    )

    assert _counts(envelope) == {
        "check": "counts_match",
        "result": "pass",
        "detail": f"left=11 right=11 key=open_issues {SOURCES}",
    }
    assert criteria_digest(CRITERIA) in envelope["policy_version"]
    # counts_match alone signs no per-step acceptance
    assert "declared_acceptance" not in [v["check"] for v in envelope["verdicts"]]
    assert verify_run_attestation(envelope).valid is True
    assert _ledger(db) == [("settled", envelope["run_id"], None, [])]
    assert (firing["state"], firing["detail"]) == (rules.SETTLED, None)


async def test_different_counts_refuse_the_firing_and_still_verify(world) -> None:
    db = world()
    firing, envelope = await _fire(
        db,
        [
            (DID, "list_issues", "call_1", _issues(11)),
            (NOTION, "query_database", "call_2", _rows(10)),
        ],
    )

    assert _counts(envelope) == {
        "check": "counts_match",
        "result": "fail",
        "detail": f"left=11 right=10 key=open_issues {SOURCES}",
    }
    assert verify_run_attestation(envelope).valid is True
    assert _ledger(db) == [("refused", None, None, [CHECK_VERDICT_FAIL])]
    assert (firing["state"], firing["detail"], firing["run_id"]) == (
        rules.REFUSED,
        CHECK_VERDICT_FAIL,
        envelope["run_id"],
    )


async def test_an_unreadable_count_refuses_the_firing(world) -> None:
    db = world()
    firing, envelope = await _fire(
        db,
        [
            (DID, "list_issues", "call_1", _issues(11)),
            (NOTION, "query_database", "call_2", '{"error": "rate limited"}'),
        ],
    )

    entry = _counts(envelope)
    assert entry["result"] == "fail"
    assert entry["detail"].startswith("left=11 right=unreadable(path not found) ")
    assert (firing["state"], firing["detail"]) == (rules.REFUSED, CHECK_VERDICT_FAIL)


async def test_a_source_never_called_refuses_the_firing(world) -> None:
    db = world()
    firing, envelope = await _fire(db, [(DID, "list_issues", "call_1", _issues(11))])

    entry = _counts(envelope)
    assert entry["result"] == "fail"
    assert entry["detail"].startswith("left=11 right=unreadable(never called) ")
    assert (firing["state"], firing["detail"]) == (rules.REFUSED, CHECK_VERDICT_FAIL)


async def test_the_last_successful_read_wins(world) -> None:
    db = world()
    firing, envelope = await _fire(
        db,
        [
            (DID, "list_issues", "call_1", _issues(11)),
            (NOTION, "query_database", "call_2", _rows(10)),
            (NOTION, "query_database", "call_3", _rows(11)),
        ],
    )

    assert _counts(envelope)["result"] == "pass"
    assert firing["state"] == rules.SETTLED


async def test_different_criteria_give_different_digests(world) -> None:
    calls: list[Call] = [
        (DID, "list_issues", "call_1", _issues(3)),
        (NOTION, "query_database", "call_2", _rows(3)),
    ]
    only = {"counts_match": True}
    both = {"counts_match": True, "citations_required": True}
    _, a = await _fire(world(only), calls)
    _, b = await _fire(world(both), calls)

    assert a["policy_version"].endswith(f"+criteria:{criteria_digest(only)}")
    assert b["policy_version"].endswith(f"+criteria:{criteria_digest(both)}")
    assert criteria_digest(only) != criteria_digest(both)
    # citations_required is judged per step, counts_match once per run
    assert _counts(b)["result"] == "pass"
    assert "declared_acceptance" in [v["check"] for v in b["verdicts"]]


async def test_operands_are_not_digested_but_are_signed(world) -> None:
    calls: list[Call] = [
        (DID, "list_issues", "call_1", _issues(3)),
        (NOTION, "query_database", "call_2", _rows(3)),
    ]
    _, a = await _fire(world(), calls)
    _, b = await _fire(world(operands={**OPERANDS, "key": "other"}), calls)

    # FR-27: the digest covers the criteria document only; the operands are
    # bound into the receipt by the signed verdict's detail.
    assert a["policy_version"] == b["policy_version"]
    assert "key=open_issues" in _counts(a)["detail"]
    assert "key=other" in _counts(b)["detail"]


# -- the pipeline's step metadata ---------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.steps: list[Any] = []

    async def on_step_complete(self, record: Any) -> None:
        self.steps.append(record)

    async def on_run_complete(self, session_id: str) -> None:
        return None


@pytest.fixture()
def recorded(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)
    recorder = Recorder()
    set_observer(recorder)
    yield recorder
    set_observer(NoOpObserver())


def _mw(criteria: dict[str, Any], connections: list[str] | None = None):
    return ExecutionMiddleware(
        state={
            "session_id": "s-1",
            "user_id": "bob",
            "routine_context": {
                "routine_id": "wf-1",
                "firing_id": "f-1",
                "connections": connections or [DID, NOTION],
                "scope_allow": [],
                "criteria_operands": {"counts_match": OPERANDS},
            },
            "_declared_criteria": criteria,
        }
    )


async def test_a_successful_step_carries_the_operands_and_no_acceptance(
    recorded,
) -> None:
    await _execute(_mw(CRITERIA), [(DID, "list_issues", "c1", _issues(1))])

    (step,) = recorded.steps
    assert step.success is True
    assert step.metadata["criteria_digest"] == criteria_digest(CRITERIA)
    assert step.metadata["run_criteria"] == {"counts_match": OPERANDS}
    assert "declared_acceptance" not in step.metadata


async def test_per_step_criteria_are_judged_without_the_run_level_ones(
    recorded,
) -> None:
    both = {"counts_match": True, "citations_required": True}
    await _execute(_mw(both), [(DID, "list_issues", "c1", _issues(1))])

    (step,) = recorded.steps
    assert step.metadata["criteria_digest"] == criteria_digest(both)
    # judged on citations only: never "unsupported criterion: counts_match"
    assert step.metadata["declared_acceptance"] == {
        "result": "fail",
        "detail": "missing citations",
    }


async def test_a_failed_dispatch_still_carries_the_operands(recorded) -> None:
    await _execute(
        _mw(CRITERIA), [(DID, "list_issues", "c1", RuntimeError("upstream 502"))]
    )

    (step,) = recorded.steps
    assert step.success is False
    assert step.metadata["criteria_digest"] == criteria_digest(CRITERIA)
    assert step.metadata["run_criteria"] == {"counts_match": OPERANDS}


async def test_a_refused_call_still_carries_the_operands(recorded) -> None:
    other = "did:orcha:agent:other-99887766"
    await _execute(
        _mw(CRITERIA, connections=[DID]), [(other, "list_issues", "c1", _issues(1))]
    )

    (step,) = recorded.steps
    assert step.success is False
    assert step.metadata["scope_approval"]["detail"] == "scope_not_allowed"
    assert step.metadata["run_criteria"] == {"counts_match": OPERANDS}


async def test_counts_match_false_carries_no_operands(recorded) -> None:
    await _execute(_mw({"counts_match": False}), [(DID, "list_issues", "c1", "x")])

    (step,) = recorded.steps
    assert step.metadata["criteria_digest"] == criteria_digest({"counts_match": False})
    assert "run_criteria" not in step.metadata
    assert "declared_acceptance" not in step.metadata


# -- chat and the routine's context ---------------------------------------------


def test_chat_still_refuses_counts_match() -> None:
    from pydantic import ValidationError
    from superagent.api.models import MessageRequest

    with pytest.raises(ValidationError, match="unsupported criterion: counts_match"):
        MessageRequest(
            session_id="s",
            user_id="u",
            message="hi",
            acceptance_criteria={"counts_match": True},
        )


def test_the_routine_context_carries_the_operands() -> None:
    from types import SimpleNamespace

    routine = SimpleNamespace(
        id="wf-1",
        agents_used=[DID],
        parameters={"scope_allow": []},
        criteria_operands={"counts_match": OPERANDS},
    )
    assert rules.routine_context(routine, "f-1")["criteria_operands"] == {
        "counts_match": OPERANDS
    }
