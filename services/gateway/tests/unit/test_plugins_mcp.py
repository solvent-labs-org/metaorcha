"""Signed-in users can list/register agents without is_dev_mode."""

from __future__ import annotations

import os
import re
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
async def client_with_mocks(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    # Story 1.3: connections sit behind CONNECTIONS_ENABLED (default off);
    # these tests exercise the route with the feature on.
    monkeypatch.setattr(settings, "connections_enabled", True)

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
    # One DID per registration (AD-15): the name's slug plus a minted suffix,
    # the same DID in the vault key and in the manifest.
    did = kwargs["json"]["agent_id"]
    assert re.fullmatch(r"did:orcha:agent:docs-mcp-[0-9a-f]{8}", did)
    assert kwargs["json"]["credentials"] == {"MCP_TOKEN": "not-a-real-token"}
    registry.post.assert_awaited_once()
    # the secret never reaches the manifest
    files = registry.post.await_args.kwargs["files"]
    assert b"not-a-real-token" not in files["emerge_yaml"][1]
    assert b"token_vault_ref: MCP_TOKEN" in files["emerge_yaml"][1]
    assert f'id: "{did}"'.encode() in files["emerge_yaml"][1]


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


# ── AD-13: stdio is operator-only on the connect door ────────────────────────

_STDIO = {
    "name": "Local MCP",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "some-mcp-server"],
}


def _no_operators(monkeypatch):
    monkeypatch.delenv("OPERATOR_USER_IDS", raising=False)
    monkeypatch.delenv("DISABLE_AUTH", raising=False)


async def test_stdio_refused_for_a_non_operator(client_with_mocks, monkeypatch):
    _no_operators(monkeypatch)
    ac, registry, headers, superagent = client_with_mocks
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_STDIO)
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("stdio_operator_only")
    registry.post.assert_not_awaited()
    superagent.post.assert_not_awaited()


async def test_stdio_allowed_for_a_listed_operator(client_with_mocks, monkeypatch):
    _no_operators(monkeypatch)
    monkeypatch.setenv("OPERATOR_USER_IDS", "someone-else, user-001")
    ac, registry, headers, _ = client_with_mocks
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_STDIO)
    assert resp.status_code == 201
    registry.post.assert_awaited_once()


async def test_stdio_allowed_when_auth_is_disabled(client_with_mocks, monkeypatch):
    _no_operators(monkeypatch)
    monkeypatch.setenv("DISABLE_AUTH", "true")
    ac, registry, headers, _ = client_with_mocks
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_STDIO)
    assert resp.status_code == 201
    registry.post.assert_awaited_once()


async def test_sse_is_not_operator_gated(client_with_mocks, monkeypatch):
    _no_operators(monkeypatch)
    ac, registry, headers, _ = client_with_mocks
    resp = await ac.post(
        "/api/v1/plugins/mcp",
        headers=headers,
        json={"name": "Docs MCP", "transport": "sse", "endpoint": "https://x.io/mcp"},
    )
    assert resp.status_code == 201
    registry.post.assert_awaited_once()


# -- story 1.3 ---------------------------------------------------------------


async def test_connect_is_refused_while_connections_are_off(
    client_with_mocks, monkeypatch
):
    from gateway.config import settings

    ac, registry, headers, superagent = client_with_mocks
    monkeypatch.setattr(settings, "connections_enabled", False)
    resp = await ac.post("/api/v1/plugins/mcp", headers=headers, json=_MCP_WITH_AUTH)
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("connections_disabled")
    superagent.post.assert_not_awaited()
    registry.post.assert_not_awaited()


async def test_a_guest_cannot_store_a_credential(client_with_mocks):
    from gateway.auth.jwt import create_access_token

    ac, registry, _, superagent = client_with_mocks
    token, _ = create_access_token(
        user_id="guest-001", email="guest@example.com", guest=True
    )
    resp = await ac.post(
        "/api/v1/plugins/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json=_MCP_WITH_AUTH,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("require_member")
    superagent.post.assert_not_awaited()
    registry.post.assert_not_awaited()


async def test_the_same_name_twice_is_two_connections(client_with_mocks):
    ac, _, headers, superagent = client_with_mocks
    for _ in range(2):
        resp = await ac.post(
            "/api/v1/plugins/mcp", headers=headers, json=_MCP_WITH_AUTH
        )
        assert resp.status_code == 201
    dids = [c.kwargs["json"]["agent_id"] for c in superagent.post.await_args_list]
    assert len(set(dids)) == 2


async def test_a_connection_manifest_is_tagged(client_with_mocks):
    ac, registry, headers, _ = client_with_mocks
    await ac.post("/api/v1/plugins/mcp", headers=headers, json=_MCP_WITH_AUTH)
    yaml_bytes = registry.post.await_args.kwargs["files"]["emerge_yaml"][1]
    assert b"    - connection\n" in yaml_bytes
