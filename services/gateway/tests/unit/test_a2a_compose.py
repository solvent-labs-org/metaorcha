"""A2A compose requires at least two agent ids."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")


@pytest_asyncio.fixture
async def client_with_mocks():
    from gateway.auth.jwt import create_access_token
    from gateway.main import app

    redis = AsyncMock()
    redis.sismember = AsyncMock(return_value=False)
    db = MagicMock()
    db.workflowtemplate = AsyncMock()

    app.state.redis = redis
    app.state.db = db
    app.state.superagent = AsyncMock()
    app.state.registry = AsyncMock()

    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, db, headers


async def test_compose_rejects_one_agent(client_with_mocks):
    ac, _db, headers = client_with_mocks
    resp = await ac.post(
        "/api/v1/workflows/compose",
        headers=headers,
        json={"name": "solo", "agent_ids": ["did:orcha:agent:one"]},
    )
    assert resp.status_code == 422


async def test_compose_two_agents(client_with_mocks):
    ac, db, headers = client_with_mocks
    rec = MagicMock()
    rec.id = "wf-1"
    rec.name = "pair"
    rec.description = "d"
    rec.goal_template = "Use these agents together: a, b"
    rec.status = "active"
    rec.agents_used = ["did:orcha:agent:a", "did:orcha:agent:b"]
    rec.steps = [{"kind": "a2a_compose"}]
    rec.run_count = 0
    rec.created_at = rec.updated_at = __import__("datetime").datetime.now(
        __import__("datetime").UTC
    )
    db.workflowtemplate.create = AsyncMock(return_value=rec)
    resp = await ac.post(
        "/api/v1/workflows/compose",
        headers=headers,
        json={
            "name": "pair",
            "agent_ids": ["did:orcha:agent:a", "did:orcha:agent:b"],
        },
    )
    assert resp.status_code == 201
    db.workflowtemplate.create.assert_awaited()
