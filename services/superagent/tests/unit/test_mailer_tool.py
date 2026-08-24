"""Mailer system tool — fixed receipt template, guardrails, Resend send path."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from superagent.api.audit import build_run_audit
from superagent.system_tools import mailer
from superagent.system_tools.registry import SystemToolRegistry


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
    }
    base.update(overrides)
    return base


def _audit_rows() -> list[SimpleNamespace]:
    t0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 31, 12, 0, 2, tzinfo=UTC)
    t2 = datetime(2026, 7, 31, 12, 0, 5, tzinfo=UTC)
    return [
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
                agent_id="did:orcha:agent:search-agent",
                protocol="MCP",
                verified=False,
                verdict_reason="Error: boom",
                total_cost_usd="0.02",
            ),
            created_at=t2,
        ),
    ]


class _FakeRedis:
    def __init__(self, stored: str | None = None) -> None:
        self.stored = stored
        self.set_calls: list[tuple[str, str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.stored

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls.append((key, value, ex or 0))
        self.stored = value


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeHttpxClient:
    """Captures POSTs instead of hitting the network."""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> _FakeHttpxClient:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        _FakeHttpxClient.calls.append({"url": url, **kwargs})
        return _FakeResponse()


@pytest.fixture
def mailer_env(monkeypatch: pytest.MonkeyPatch):
    """Handler wired to fake redis, fake transcript rows, fake httpx."""
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    fake_redis = _FakeRedis()
    monkeypatch.setattr(mailer, "_redis_client", fake_redis)

    async def _fake_rows(session_id: str) -> list[SimpleNamespace]:
        return _audit_rows()

    monkeypatch.setattr(
        "superagent.persistence.transcript_store.load_transcript_rows",
        _fake_rows,
    )
    _FakeHttpxClient.calls = []
    monkeypatch.setattr(mailer.httpx, "AsyncClient", _FakeHttpxClient)
    return fake_redis


# ── template rendering ────────────────────────────────────────────────────────


def test_render_receipt_includes_goal_steps_and_summary():
    audit = build_run_audit("sess-1", _audit_rows())
    body = mailer._render_receipt(audit)

    assert "Goal: Show me my portfolio" in body
    assert "1. finance-dashboard · A2A · verified · ok · $0.01" in body
    assert "2. search-agent · MCP · failed · Error: boom · $0.02" in body
    assert "Summary: 2 steps — 1 verified, 1 failed" in body
    assert "Total cost: $0.03" in body
    assert "Duration: 5000 ms" in body
    assert body.rstrip().endswith(
        "Run it yourself: https://github.com/solvent-labs-org/metaorcha"
    )


def test_render_receipt_truncates_goal_and_omits_missing_cost():
    rows = [
        _row("USER", "x" * 300),
        _row("TOOL", tool_inputs=_meta(protocol="", verified=True)),
    ]
    body = mailer._render_receipt(build_run_audit("sess-2", rows))

    goal_line = next(line for line in body.splitlines() if line.startswith("Goal: "))
    assert len(goal_line) == len("Goal: ") + 200
    step_line = next(line for line in body.splitlines() if "finance-dashboard" in line)
    assert "$" not in step_line
    assert " · — · " in step_line  # empty protocol renders as dash


# ── guardrails ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_email_rejected(mailer_env):
    result = await mailer._send_run_receipt(
        {"to_email": "not-an-email", "session_id": "sess-1"},
        {"user_id": "u1"},
    )
    assert result == "Error: invalid email address"
    assert _FakeHttpxClient.calls == []


@pytest.mark.asyncio
async def test_unconfigured_key_returns_error(mailer_env, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    result = await mailer._send_run_receipt(
        {"to_email": "visitor@example.com", "session_id": "sess-1"},
        {"user_id": "u1"},
    )
    assert result == "Error: mailer not configured"
    assert _FakeHttpxClient.calls == []


@pytest.mark.asyncio
async def test_over_cap_returns_cap_error(mailer_env):
    mailer_env.stored = "1"  # cap key already set for today
    result = await mailer._send_run_receipt(
        {"to_email": "visitor@example.com", "session_id": "sess-1"},
        {"user_id": "u1"},
    )
    assert result == "Error: receipt email limit reached (1/day)"
    assert _FakeHttpxClient.calls == []


# ── send path ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_success_posts_to_resend_and_sets_cap(mailer_env):
    result = await mailer._send_run_receipt(
        {"to_email": "visitor@example.com", "session_id": "sess-1"},
        {"user_id": "u1"},
    )

    assert result == "Receipt sent to visitor@example.com for session sess-1"

    assert len(_FakeHttpxClient.calls) == 1
    call = _FakeHttpxClient.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer test-resend-key"
    payload = call["json"]
    assert payload["from"] == "Orcha Sandbox <receipts@orcha.ai>"
    assert payload["to"] == ["visitor@example.com"]
    assert payload["subject"] == "Your Orcha run receipt"
    assert "Goal: Show me my portfolio" in payload["text"]

    assert len(mailer_env.set_calls) == 1
    key, value, ttl = mailer_env.set_calls[0]
    assert key.startswith("mailer:cap:u1:")
    assert value == "1"
    assert ttl == 86400


@pytest.mark.asyncio
async def test_send_failure_returns_error_string(mailer_env, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("resend down")

    monkeypatch.setattr(mailer, "_send_email", boom)
    result = await mailer._send_run_receipt(
        {"to_email": "visitor@example.com", "session_id": "sess-1"},
        {"user_id": "u1"},
    )
    assert result == "Error: failed to send receipt"
    assert mailer_env.set_calls == []  # cap not consumed on failure


# ── registration gate ─────────────────────────────────────────────────────────


def test_registration_gated_on_sandbox_mailer(monkeypatch):
    from superagent.config import settings

    registry = SystemToolRegistry()

    monkeypatch.setattr(settings, "sandbox_mailer", False)
    mailer.register_mailer_tools(registry)
    assert registry.has("send_run_receipt") is False

    monkeypatch.setattr(settings, "sandbox_mailer", True)
    mailer.register_mailer_tools(registry)
    assert registry.has("send_run_receipt") is True
    schema = registry.get_all_schemas()[0]["function"]
    assert schema["parameters"]["required"] == ["to_email", "session_id"]
