"""Verified Runs: audit assembly + verdict transcript meta."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

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
    assert "Orcha Verified Runs" in audit.note


def test_audit_defaults_for_preexisting_transcripts():
    """Rows persisted before verdict meta existed default to verified/ok."""
    rows = [
        _row("USER", "goal"),
        _row("TOOL", tool_inputs=_meta()),
    ]
    audit = build_run_audit("sess-2", rows)

    assert audit.summary.total_steps == 1
    assert audit.summary.steps_verified == 1
    assert audit.steps[0].verified is True
    assert audit.steps[0].verdict_reason == "ok"
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


class _FakeSettlements:
    def __init__(self, row=None, exc=None):
        self.row, self.exc, self.calls = row, exc, []

    async def find_first(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.row


class _FakeDb:
    def __init__(self, row=None, exc=None):
        self.attestedsettlement = _FakeSettlements(row, exc)


async def test_load_gate_outcome_reads_latest_row_for_session():
    from superagent.api.audit import load_gate_outcome

    row = SimpleNamespace(
        outcome="refused",
        failed_checks=["steps_root", 7, None],
        envelope_digest="cd" * 32,
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
    )
    db = _FakeDb(row=row)
    gate = await load_gate_outcome("sess-1", db)
    assert gate is not None
    assert gate.outcome == "refused"
    assert gate.failed_checks == ["steps_root"]  # non-strings dropped
    assert gate.created_at.startswith("2026-09-25")
    where = db.attestedsettlement.calls[0]["where"]
    assert where == {"session_id": "sess-1"}
    assert db.attestedsettlement.calls[0]["order"] == {"created_at": "desc"}


async def test_load_gate_outcome_none_when_no_row_or_on_error():
    from superagent.api.audit import load_gate_outcome

    assert await load_gate_outcome("sess-1", _FakeDb(row=None)) is None
    assert await load_gate_outcome("sess-1", _FakeDb(exc=RuntimeError("db"))) is None


async def test_load_gate_outcome_logs_a_request_id_on_one_line(caplog):
    # the id comes from the request path; a line break in it must not start
    # a second, forged log line
    from superagent.api.audit import load_gate_outcome

    forged = "sess-1\nWARNING forged\r"
    with caplog.at_level(logging.WARNING, logger="superagent.api.audit"):
        assert await load_gate_outcome(forged, _FakeDb(exc=RuntimeError("db"))) is None
    (message,) = [
        r.getMessage() for r in caplog.records if r.name == "superagent.api.audit"
    ]
    assert "\n" not in message and "\r" not in message
    assert message.endswith("sess-1\\nWARNING forged\\r")
