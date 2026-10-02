"""Story 2.1: save a routine — validated before anything is persisted.

``POST /api/v1/workflows/routines``. The Gateway checks the flag, the cron and
the time zone itself, then asks the SuperAgent (which owns the scope-class
rules and the criteria vocabulary) to accept the connections, allows and
criteria. A refusal anywhere means no row is written.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

from .office_db import FakeDB

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

DID = "did:orcha:agent:docs-mcp-0a1b2c3d"
URL = "/api/v1/workflows/routines"


def _body(**over) -> dict:
    body = {
        "name": "Weekly digest",
        "goal": "Summarise this week's open issues with citations",
        "connections": [DID],
        "scope_allow": [f"{DID}#create_comment"],
        "model": "openrouter/some-model",
        "criteria": {"citations_required": True},
        "criteria_operands": {"citations_required": {"min": 2}},
        "cron": "0 9 * * 1",
        "timezone": "Europe/London",
    }
    body.update(over)
    return body


class _Redis:
    async def sismember(self, _key: str, _member: str) -> bool:
        return False  # the JWT revocation set (require_auth)


def _record(data: dict) -> SimpleNamespace:
    # what Prisma hands back: Json columns deserialised to plain values
    data = {k: getattr(v, "data", v) for k, v in data.items()}
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return SimpleNamespace(
        **{"id": "wf-1", "run_count": 0, "created_at": now, "updated_at": now, **data}
    )


@pytest_asyncio.fixture
async def gw(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    monkeypatch.setattr(settings, "connections_enabled", True)
    superagent = MagicMock()
    superagent.post = AsyncMock(
        return_value=Response(200, json={"classes": {f"{DID}#create_comment": "write"}})
    )
    db = FakeDB()
    db.add_connection(DID, "user-001", "po_user-001")
    db.workflowtemplate.create = AsyncMock(side_effect=_record)
    app.state.superagent = superagent
    app.state.db = db
    app.state.redis = _Redis()
    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, headers, superagent, db


@pytest.mark.asyncio
async def test_a_valid_routine_persists_all_seven_fields(gw) -> None:
    ac, headers, superagent, db = gw
    resp = await ac.post(URL, json=_body(), headers=headers)
    assert resp.status_code == 201, resp.text
    superagent.post.assert_awaited_once_with(
        "/routines/validate",
        json={
            "connections": [DID],
            "scope_allow": [f"{DID}#create_comment"],
            "criteria": {"citations_required": True},
            "criteria_operands": {"citations_required": {"min": 2}},
        },
    )
    from common.database.src.generated_client.fields import Json  # the conftest stub

    data = db.workflowtemplate.create.await_args.kwargs["data"]
    for key in ("parameters", "steps", "criteria", "criteria_operands"):
        assert isinstance(data[key], Json), key  # a raw dict is a relation input
    data = {k: getattr(v, "data", v) for k, v in data.items()}
    assert data["user_id"] == "user-001"
    assert data["goal_template"] == _body()["goal"]
    assert data["agents_used"] == [DID]
    assert data["parameters"] == {
        "scope_allow": [f"{DID}#create_comment"],
        "model": "openrouter/some-model",
    }
    assert data["criteria"] == {"citations_required": True}
    assert data["criteria_operands"] == {"citations_required": {"min": 2}}
    assert (data["schedule_cron"], data["schedule_tz"]) == (
        "0 9 * * 1",
        "Europe/London",
    )
    # recorded, not yet firing: the scheduler is story 2.2
    assert data["schedule_enabled"] is False
    assert data["status"] == "inactive"
    out = resp.json()
    assert out["model"] == "openrouter/some-model"
    assert out["scope_allow"] == [f"{DID}#create_comment"]
    assert out["criteria"] == {"citations_required": True}
    assert out["schedule_cron"] == "0 9 * * 1"


@pytest.mark.asyncio
async def test_a_destructive_allow_is_refused_and_nothing_is_persisted(gw) -> None:
    ac, headers, superagent, db = gw
    reason = (
        "delete_repo is destructive and can never be allowed on a routine "
        "(a destructive call always waits for a person)"
    )
    superagent.post.return_value = Response(
        422, json={"detail": {"field": "scope_allow", "reason": reason}}
    )
    resp = await ac.post(
        URL, json=_body(scope_allow=[f"{DID}#delete_repo"]), headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"field": "scope_allow", "reason": reason}
    db.workflowtemplate.create.assert_not_awaited()


@pytest.mark.parametrize(
    ("cron", "tz", "field"),
    [
        ("61 9 * * 1", "UTC", "cron"),
        ("every monday", "UTC", "cron"),
        ("0 9 * * 1", "Mars/Olympus", "timezone"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_schedule_names_the_field_before_anything_else(
    gw, cron, tz, field
) -> None:
    ac, headers, superagent, db = gw
    resp = await ac.post(URL, json=_body(cron=cron, timezone=tz), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == field
    superagent.post.assert_not_awaited()
    db.workflowtemplate.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_unavailable_saves_nothing(gw) -> None:
    ac, headers, superagent, db = gw
    superagent.post.return_value = Response(500, text="boom")
    resp = await ac.post(URL, json=_body(), headers=headers)
    assert resp.status_code == 502
    db.workflowtemplate.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_connections_turned_off_refuses_the_save(gw, monkeypatch) -> None:
    from gateway.config import settings

    monkeypatch.setattr(settings, "connections_enabled", False)
    ac, headers, superagent, db = gw
    resp = await ac.post(URL, json=_body(), headers=headers)
    assert resp.status_code == 403
    assert "connections_disabled" in resp.text
    superagent.post.assert_not_awaited()
    db.workflowtemplate.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_guest_cannot_save_a_routine(gw) -> None:
    from gateway.auth.jwt import create_access_token

    ac, _, superagent, db = gw
    token, _ = create_access_token(
        user_id="guest-1", email="guest@example.com", guest=True
    )
    resp = await ac.post(
        URL, json=_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    superagent.post.assert_not_awaited()
    db.workflowtemplate.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_routine_needs_at_least_one_connection(gw) -> None:
    ac, headers, superagent, db = gw
    resp = await ac.post(URL, json=_body(connections=[]), headers=headers)
    assert resp.status_code == 422
    superagent.post.assert_not_awaited()
    db.workflowtemplate.create.assert_not_awaited()
