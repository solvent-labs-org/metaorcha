"""Signed-in users can list/register agents without is_dev_mode."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

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
    registry = AsyncMock()
    registry.request = AsyncMock(
        return_value=Response(200, json={"status": "success", "data": {"agents": []}})
    )
    registry.post = AsyncMock(
        return_value=Response(
            201,
            json={
                "status": "success",
                "data": {"agent_id": "did:orcha:agent:docs-mcp", "name": "Docs MCP"},
            },
        )
    )

    app.state.redis = redis
    app.state.registry = registry
    app.state.superagent = AsyncMock()
    app.state.db = MagicMock()

    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, registry, headers


async def test_list_agents_without_dev_mode(client_with_mocks):
    ac, registry, headers = client_with_mocks
    resp = await ac.get("/api/v1/dev/agents", headers=headers)
    assert resp.status_code == 200
    registry.request.assert_awaited()


async def test_connect_mcp_json(client_with_mocks):
    ac, registry, headers = client_with_mocks
    resp = await ac.post(
        "/api/v1/plugins/mcp",
        headers=headers,
        json={
            "name": "Docs MCP",
            "transport": "sse",
            "endpoint": "https://example.com/mcp",
        },
    )
    assert resp.status_code == 201
    registry.post.assert_awaited()
