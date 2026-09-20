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

    superagent = AsyncMock()
    superagent.post = AsyncMock(return_value=Response(200, json={"status": "ok"}))

    app.state.redis = redis
    app.state.registry = registry
    app.state.superagent = superagent
    app.state.db = MagicMock()

    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, registry, headers, superagent


async def test_list_agents_without_dev_mode(client_with_mocks):
    ac, registry, headers, _ = client_with_mocks
    resp = await ac.get("/api/v1/dev/agents", headers=headers)
    assert resp.status_code == 200
    registry.request.assert_awaited()


async def test_connect_mcp_json(client_with_mocks):
    ac, registry, headers, superagent = client_with_mocks
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
    superagent.post.assert_not_awaited()  # no credential, no vault write


_MCP_WITH_AUTH = {
    "name": "Docs MCP",
    "transport": "sse",
    "endpoint": "https://example.com/mcp",
    "auth_var": "MCP_TOKEN",
    "auth_value": "not-a-real-token",
}


async def test_connect_mcp_stores_credential_then_registers(client_with_mocks):
    ac, registry, headers, superagent = client_with_mocks
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_MCP_WITH_AUTH)
    assert resp.status_code == 201
    superagent.post.assert_awaited_once()
    _, kwargs = superagent.post.await_args
    assert kwargs["json"]["agent_id"] == "did:orcha:agent:docs-mcp"
    assert kwargs["json"]["credentials"] == {"MCP_TOKEN": "not-a-real-token"}
    registry.post.assert_awaited_once()
    # the secret never reaches the manifest
    files = registry.post.await_args.kwargs["files"]
    assert b"not-a-real-token" not in files["emerge_yaml"][1]
    assert b"token_vault_ref: MCP_TOKEN" in files["emerge_yaml"][1]


async def test_vault_write_failure_is_not_a_201(client_with_mocks):
    ac, registry, headers, superagent = client_with_mocks
    superagent.post = AsyncMock(return_value=Response(500, text="vault down"))
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_MCP_WITH_AUTH)
    assert resp.status_code == 502
    assert "not registered" in resp.json()["detail"]
    registry.post.assert_not_awaited()  # nothing registered without its credential
    assert "vault down" not in resp.text  # upstream body is not echoed


async def test_auth_var_shape_is_a_422_at_the_route(client_with_mocks):
    ac, registry, headers, superagent = client_with_mocks
    for bad in ("mcp token", "X: y", "A\nB", "1ABC"):
        resp = await ac.post(
            "/api/v1/plugins/mcp",
            headers=headers,
            json={**_MCP_WITH_AUTH, "auth_var": bad},
        )
        assert resp.status_code == 422, bad
    registry.post.assert_not_awaited()
    superagent.post.assert_not_awaited()


async def test_auth_var_and_value_come_together(client_with_mocks):
    ac, registry, headers, _ = client_with_mocks
    half = {k: v for k, v in _MCP_WITH_AUTH.items() if k != "auth_value"}
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=half)
    assert resp.status_code == 422
    half = {k: v for k, v in _MCP_WITH_AUTH.items() if k != "auth_var"}
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=half)
    assert resp.status_code == 422
    registry.post.assert_not_awaited()
