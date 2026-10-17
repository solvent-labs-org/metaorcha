"""Story 1.6b: a connection's token never lands in the Gateway (AD-14).

The connect route sends the token to exactly two places — the SuperAgent
vault write and the Registry's harvest header — and nowhere else: not the
session-credential cache in Redis, not the response, not the log.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock

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

CANARY = "canary-8f3a1c9e-connection-token-never-leaks"


@pytest_asyncio.fixture
async def client(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    monkeypatch.setattr(settings, "connections_enabled", True)
    redis = AsyncMock()
    redis.sismember = AsyncMock(return_value=False)
    registry = AsyncMock()
    registry.post = AsyncMock(
        return_value=Response(
            201,
            json={
                "status": "success",
                "data": {
                    "agent_id": "did:orcha:agent:docs-mcp-0a1b2c3d",
                    "name": "Docs",
                },
            },
        )
    )
    superagent = AsyncMock()
    superagent.post = AsyncMock(return_value=Response(204))
    app.state.redis = redis
    app.state.registry = registry
    app.state.superagent = superagent
    db = FakeDB()
    db.agent.update = AsyncMock()  # the Registry, mocked, owns the row
    app.state.db = db
    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, {"Authorization": f"Bearer {token}"}, redis, registry, superagent


@pytest.mark.asyncio
async def test_connect_sends_the_token_to_the_vault_and_the_harvest_only(
    client, caplog
) -> None:
    ac, headers, redis, registry, superagent = client
    with caplog.at_level(logging.DEBUG):
        resp = await ac.post(
            "/api/v1/plugins/mcp",
            headers=headers,
            json={
                "name": "Docs MCP",
                "transport": "sse",
                "endpoint": "https://example.com/mcp",
                "auth_var": "MCP_TOKEN",
                "auth_value": CANARY,
            },
        )
    assert resp.status_code == 201
    # positive controls: the two transports that must carry it
    assert superagent.post.await_args.kwargs["json"]["credentials"] == {
        "MCP_TOKEN": CANARY
    }
    assert registry.post.await_args.kwargs["headers"]["X-Harvest-Authorization"] == (
        f"Bearer {CANARY}"
    )
    # and nowhere else
    assert CANARY not in resp.text
    assert (
        CANARY
        not in registry.post.await_args.kwargs["files"]["emerge_yaml"][1].decode()
    )
    log_text = "\n".join(
        [caplog.text, *(r.getMessage() + repr(r.args) for r in caplog.records)]
    )
    assert CANARY not in log_text
    # the session-credential cache is not written by the connect flow
    for method in ("set", "setex", "hset", "lpush", "rpush"):
        getattr(redis, method).assert_not_awaited()
