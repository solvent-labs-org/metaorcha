"""Verified Runs: audit assembly + verdict transcript meta."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from superagent.api.audit import build_run_audit
from superagent.nodes.execute_agent_calls import _tool_transcript_meta
from superagent.persistence.transcript_store import TRANSCRIPT_TOOL_META_KEY


def _row(
    role: str,
    content: str = "",
    tool_inputs: dict | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        content=content,
        tool_inputs=tool_inputs,
        created_at=created_at,
    )


def _meta(**overrides) -> dict:
    base = {
        "agent_id": "did:orcha:agent:finance-dashboard",
        "capability_id": "get_portfolio",
        "protocol": "A2A",
        "internal_tool_name": "finance_dashboard__get_portfolio",
        "invocation_args": {"api_key": "secret-value"},
    }
    base.update(overrides)
    return base


# ── build_run_audit ───────────────────────────────────────────────────────────


def test_audit_summary_counts_and_protocols():
    t0 = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 26, 12, 0, 2, tzinfo=UTC)
    t2 = datetime(2026, 7, 26, 12, 0, 5, tzinfo=UTC)
    rows = [
        _row("USER", "Show me my portfolio", created_at=t0),
        _row(
            "TOOL",
            tool_inputs=_meta(
                verified=True, verdict_reason="ok", total_cost_usd="0.01"
            ),
            created_at=t1,
        ),
        _row(
            "TOOL",
            tool_inputs=_meta(
                protocol="MCP",
                verified=False,
                verdict_reason="Error: boom",
                total_cost_usd="0.02",
            ),
            created_at=t2,
        ),
    ]
    audit = build_run_audit("sess-1", rows, generated_at=t2)

    assert audit.session_id == "sess-1"
    assert audit.goal == "Show me my portfolio"
    assert audit.summary.total_steps == 2
    assert audit.summary.steps_verified == 1
    assert audit.summary.steps_failed == 1
    assert audit.summary.protocols == ["A2A", "MCP"]
    assert audit.summary.total_cost_usd == "0.03"
    assert audit.summary.duration_ms == 5000
    assert audit.steps[0].seq == 1
    assert audit.steps[1].verdict_reason == "Error: boom"
    assert "Metaorcha" in audit.note


def test_a_step_with_no_verdict_is_unchecked_never_verified():
    """A blocked, unresolved or system call, or a row persisted before verdicts
    existed, carries no structural verdict: it is exported unchecked, and the
    summary never counts it as verified (story 2.6)."""
    rows = [
        _row("USER", "goal"),
        _row("TOOL", tool_inputs=_meta()),
    ]
    audit = build_run_audit("sess-2", rows)

    assert audit.summary.total_steps == 1
    assert audit.summary.steps_verified == 0
    assert audit.summary.steps_failed == 0
    assert audit.summary.steps_unchecked == 1
    assert audit.steps[0].verified is None
    assert audit.steps[0].verdict_reason == ""
    assert "verified" not in audit.model_dump(exclude_none=True)["steps"][0]
    assert audit.steps[0].base_fee is None
    assert audit.steps[0].total_cost_usd is None
    assert audit.summary.total_cost_usd == "0"
    assert audit.summary.duration_ms is None


def test_audit_omits_invocation_args():
    rows = [_row("TOOL", tool_inputs=_meta(verified=True))]
    audit = build_run_audit("sess-3", rows)

    dumped = audit.steps[0].model_dump()
    assert "invocation_args" not in dumped
    assert "secret-value" not in audit.model_dump_json()


def test_audit_skips_non_tool_and_metaless_rows():
    rows = [
        _row("USER", "hello"),
        _row("ASSISTANT", "thinking…"),
        _row("TOOL", tool_inputs=None),
        _row("TOOL", tool_inputs={"unexpected": "shape"}),
    ]
    audit = build_run_audit("sess-4", rows)
    assert audit.summary.total_steps == 0
    assert audit.steps == []


def test_audit_goal_truncated():
    rows = [_row("USER", "x" * 600)]
    audit = build_run_audit("sess-5", rows)
    assert len(audit.goal) == 500


# ── _tool_transcript_meta verdict fields ─────────────────────────────────────


def test_transcript_meta_includes_verdict_fields_when_passed():
    meta = _tool_transcript_meta(
        agent_id="did:orcha:agent:x",
        verified=False,
        verdict_reason="Error: boom",
        total_cost_usd="0.05",
    )
    assert meta["verified"] is False
    assert meta["verdict_reason"] == "Error: boom"
    assert meta["total_cost_usd"] == "0.05"


def test_transcript_meta_omits_verdict_fields_by_default():
    meta = _tool_transcript_meta(agent_id="did:orcha:agent:x")
    assert "verified" not in meta
    assert "verdict_reason" not in meta
    assert "total_cost_usd" not in meta


def test_transcript_meta_omits_zero_cost():
    meta = _tool_transcript_meta(agent_id="did:orcha:agent:x", total_cost_usd="0")
    assert "total_cost_usd" not in meta


def test_transcript_meta_key_round_trip():
    """Meta dict lands under TRANSCRIPT_TOOL_META_KEY on ToolMessage kwargs."""
    meta = _tool_transcript_meta(agent_id="a", verified=True, verdict_reason="ok")
    additional_kwargs = {TRANSCRIPT_TOOL_META_KEY: meta}
    assert additional_kwargs[TRANSCRIPT_TOOL_META_KEY]["verified"] is True


# ── gate outcome (story 1.1, FR-14) ───────────────────────────────────────────


def test_audit_gate_is_absent_unless_a_gate_evaluated_the_run():
    audit = build_run_audit("s1", [_row("USER", "goal")])
    assert audit.gate is None
    assert "gate" not in audit.model_dump(exclude_none=True)


def test_audit_gate_passes_through_named_checks():
    from superagent.api.models import RunAuditGate

    gate = RunAuditGate(
        outcome="refused",
        failed_checks=["signature"],
        envelope_digest="ab" * 32,
        created_at="2026-09-25T00:00:00+00:00",
    )
    audit = build_run_audit("s1", [_row("USER", "goal")], gate=gate)
    assert audit.gate is not None
    assert audit.gate.outcome == "refused"
    assert audit.gate.failed_checks == ["signature"]


# ── settlement evidence (story 2.6) ───────────────────────────────────────────


def _db():
    from .test_routine_scheduler import DB

    return DB()


def _envelope(run_id: str, verdicts: list[dict] | None = None) -> dict:
    return {"run_id": run_id, "verdicts": verdicts or []}


def _seal(db, session_id: str, run_id: str | None, verdicts=None) -> None:
    db.attestation.rows.append(
        {
            "id": f"att-{run_id}",
            "session_id": session_id,
            "run_id": run_id,
            "payload": _envelope(run_id or "case", verdicts),
        }
    )


def _ledger(db, run_id: str, outcome: str, checks=(), call_id=None, **extra) -> None:
    db.attestedsettlement.rows.append(
        {
            "id": f"led-{len(db.attestedsettlement.rows)}",
            "run_id": run_id,
            "outcome": outcome,
            "failed_checks": list(checks),
            "call_id": call_id,
            "envelope_digest": "ab" * 32,
            "created_at": datetime(2026, 10, 3, tzinfo=UTC),
            **extra,
        }
    )


def _firing(db, session_id: str, state: str, run_id=None, detail=None, criteria=None):
    db.workflowtemplate.rows.append({"id": "rt-1", "criteria": criteria or {}})
    db.routinefiring.rows.append(
        {
            "id": "f-1",
            "routine_id": "rt-1",
            "session_id": session_id,
            "state": state,
            "detail": detail,
            "run_id": run_id,
        }
    )


_DECISION_WORDS = re.compile(
    r"\b(settled|refused|charged|paid|withheld|evaluated)\b", re.IGNORECASE
)
_COUNTS_FAIL = {
    "check": "counts_match",
    "result": "fail",
    "detail": "left=11 right=10 key=open_issues",
}


async def test_an_unjudged_firing_exports_attested_but_unsettled_and_nothing_more():
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(db, "s1", "attested_unsettled", run_id="r1")
    _seal(db, "s1", "r1")

    evidence = await load_settlement("s1", db)
    audit = build_run_audit("s1", [_row("USER", "goal")], evidence=evidence)

    assert evidence.gate is None
    assert audit.run_id == "r1"
    block = audit.model_dump(exclude_none=True)["settlement"]
    assert block["label"] == "attested but unsettled"
    assert block["gate_evaluated"] is False
    assert block["checks_label"] == "recorded, unchecked"
    assert not {"verdict_only", "failed_checks", "failed_verdicts"} & set(block)
    assert not _DECISION_WORDS.search(f"{block['label']} {block['statement']}")


async def test_an_unreconciled_firing_never_says_no_decision_is_recorded():
    """The row says attested_unsettled, the ledger holds the gate's row: the
    export shows the row through the run-keyed gate and no settlement block."""
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(db, "s1", "attested_unsettled", run_id="r1")
    _seal(db, "s1", "r1")
    _ledger(db, "r1", "refused", ["verdict_fail"])

    evidence = await load_settlement("s1", db)
    assert evidence.settlement is None
    assert evidence.gate is not None and evidence.gate.outcome == "refused"


@pytest.mark.parametrize(
    ("call_id", "label", "verdict_only", "charged"),
    [
        (None, "settled — verdict only, nothing charged", True, False),
        ("c1", "settled", False, True),
    ],
)
async def test_a_settled_firing_says_whether_its_decision_moved_money(
    call_id, label, verdict_only, charged
):
    from superagent.api.audit import load_settlement

    from common.utils.src import firing_view

    db = _db()
    _firing(db, "s1", "settled", run_id="r1")
    _seal(db, "s1", "r1")
    _ledger(db, "r1", "settled", call_id=call_id)

    evidence = await load_settlement("s1", db)
    assert evidence.settlement is not None
    assert evidence.settlement.label == label
    assert evidence.settlement.verdict_only is verdict_only
    assert evidence.settlement.gate_evaluated is True
    assert evidence.gate is not None and evidence.gate.charged is charged
    assert evidence.settlement.statement == (
        firing_view.STATEMENT_VERDICT_ONLY
        if verdict_only
        else firing_view.STATEMENT_CHARGED
    )


async def test_a_refused_firing_names_its_check_and_carries_the_failed_verdict():
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(
        db,
        "s1",
        "refused",
        run_id="r1",
        detail="counts_match",
        criteria={"counts_match": True},
    )
    _seal(db, "s1", "r1", [_COUNTS_FAIL])
    _ledger(db, "r1", "refused", ["verdict_fail"])

    evidence = await load_settlement("s1", db)
    s = evidence.settlement
    assert s is not None
    assert s.label == "refused — counts_match — verdict only, nothing charged"
    assert s.failed_checks == ["verdict_fail"]
    assert s.failed_verdicts == [
        {"check": "counts_match", "detail": _COUNTS_FAIL["detail"]}
    ]
    assert (s.checks, s.checks_label) == ("checked", "checked: counts_match")
    assert evidence.firing is not None
    assert evidence.firing.label == "refused — counts_match"


async def test_a_chat_session_export_has_no_settlement_block():
    from superagent.api.audit import load_settlement

    db = _db()
    _seal(db, "s1", "r1")

    evidence = await load_settlement("s1", db)
    assert (evidence.run_id, evidence.settlement, evidence.firing, evidence.gate) == (
        "r1",
        None,
        None,
        None,
    )


async def test_the_gate_block_is_keyed_by_run_not_by_session():
    """A later, unjudged run never inherits an earlier run's gate row."""
    from superagent.api.audit import load_settlement

    db = _db()
    _seal(db, "s1", "r1")
    _ledger(db, "r1", "settled", session_id="s1")
    _seal(db, "s1", "r2")

    evidence = await load_settlement("s1", db)
    assert evidence.run_id == "r2"
    assert evidence.gate is None


async def test_the_export_skips_a_case_attestation():
    from superagent.api.audit import load_settlement

    db = _db()
    _seal(db, "s1", "r1")
    _seal(db, "s1", None)  # a case attestation: run_id NULL, not a seal

    evidence = await load_settlement("s1", db)
    assert evidence.run_id == "r1"


@pytest.mark.parametrize("state", ["error", "paused", "running"])
async def test_a_firing_export_reads_the_row_state(state):
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(db, "s1", state, detail="restart" if state == "error" else None)
    _seal(db, "s1", "r1")

    evidence = await load_settlement("s1", db)
    assert evidence.firing is not None and evidence.firing.state == state
    assert evidence.settlement is None


async def test_the_export_and_the_pane_agree_that_false_declares_nothing():
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(
        db,
        "s1",
        "attested_unsettled",
        run_id="r1",
        criteria={"citations_required": False},
    )
    _seal(db, "s1", "r1")

    evidence = await load_settlement("s1", db)
    assert evidence.settlement is not None
    assert evidence.settlement.checks_label == "recorded, unchecked"


async def test_load_settlement_degrades_to_saying_nothing_on_a_db_error():
    from superagent.api.audit import load_settlement

    db = _db()

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    db.routinefiring.find_unique = boom
    evidence = await load_settlement("s1", db)
    assert (evidence.run_id, evidence.gate, evidence.settlement, evidence.firing) == (
        None,
        None,
        None,
        None,
    )


async def test_load_settlement_logs_a_request_id_on_one_line(caplog):
    # the id comes from the request path; a line break in it must not start
    # a second, forged log line
    from superagent.api.audit import load_settlement

    db = _db()

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    db.routinefiring.find_unique = boom
    forged = "sess-1\nWARNING forged\r"
    with caplog.at_level(logging.WARNING, logger="superagent.api.audit"):
        evidence = await load_settlement(forged, db)
    assert evidence.run_id is None
    (message,) = [
        r.getMessage() for r in caplog.records if r.name == "superagent.api.audit"
    ]
    assert "\n" not in message and "\r" not in message
    assert message.endswith("sess-1\\nWARNING forged\\r")


def test_the_receipt_email_carries_the_settlement_line_only_for_a_firing():
    from superagent.api.models import RunAuditSettlement
    from superagent.system_tools.mailer import _render_receipt

    chat = build_run_audit("s1", [_row("USER", "goal")])
    assert "Settlement:" not in _render_receipt(chat)

    firing = chat.model_copy(
        update={
            "settlement": RunAuditSettlement(
                run_id="r1",
                state="refused",
                label="refused — counts_match — verdict only, nothing charged",
                gate_evaluated=True,
                statement="x",
            )
        }
    )
    assert (
        "Settlement: refused — counts_match — verdict only, nothing charged"
        in _render_receipt(firing)
    )


def test_an_unchecked_step_is_not_mailed_as_verified():
    from superagent.system_tools.mailer import _render_receipt

    audit = build_run_audit(
        "s1", [_row("USER", "goal"), _row("TOOL", tool_inputs=_meta())]
    )
    body = _render_receipt(audit)
    assert "not checked" in body
    assert "0 verified, 0 failed, 1 not checked" in body


async def test_a_firing_session_that_went_on_as_chat_says_no_settlement():
    """Chat after the firing sealed r2 (charged) in the same session: the
    firing's "nothing charged" would sit beside a total it does not describe,
    so no settlement block; the gate is the newest run's."""
    from superagent.api.audit import load_settlement
    from superagent.system_tools.mailer import _render_receipt

    db = _db()
    _firing(db, "s1", "settled", run_id="r1")
    _seal(db, "s1", "r1")
    _ledger(db, "r1", "settled")
    _seal(db, "s1", "r2")
    _ledger(db, "r2", "settled", call_id="c1")

    evidence = await load_settlement("s1", db)
    assert evidence.settlement is None
    assert evidence.run_id == "r2"
    assert evidence.gate is not None and evidence.gate.charged is True
    assert evidence.firing is not None and evidence.firing.label == "settled"
    audit = build_run_audit("s1", [_row("USER", "goal")], evidence=evidence)
    assert "nothing charged" not in _render_receipt(audit)


async def test_the_firings_own_run_is_read_when_it_is_the_newest_seal():
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(db, "s1", "settled", run_id="r1")
    _seal(db, "s1", None)  # a case attestation after it is not a seal
    _seal(db, "s1", "r1")
    _ledger(db, "r1", "settled")

    evidence = await load_settlement("s1", db)
    assert evidence.run_id == "r1"
    assert evidence.settlement is not None
    assert evidence.settlement.state == "settled"


async def test_an_envelope_the_gate_could_not_verify_is_no_evidence_of_checks():
    from superagent.api.audit import load_settlement

    db = _db()
    _firing(
        db,
        "s1",
        "refused",
        run_id="r1",
        detail="signature",
        criteria={"counts_match": True},
    )
    _seal(db, "s1", "r1", [_COUNTS_FAIL])
    _ledger(db, "r1", "refused", ["signature"])

    s = (await load_settlement("s1", db)).settlement
    assert s is not None
    assert s.label == "refused — signature — verdict only, nothing charged"
    assert s.failed_checks == ["signature"]
    assert (s.failed_verdicts, s.checks, s.checks_label) == (None, None, None)
