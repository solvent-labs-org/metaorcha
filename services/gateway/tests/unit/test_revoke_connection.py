"""Story 1.7: remove a whole connection — the caller's token, then the agent.

``DELETE /api/v1/plugins/mcp/{agent_id}``. The vault rows go first (only the
caller's, keyed ``agent:<DID>:env:*`` by the SuperAgent), so the next call
fails closed even if deregistration then fails; the Registry soft-deletes the
agent; a failure is an error, never a 204.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

DID = "did:orcha:agent:docs-mcp-0a1b2c3d"
OTHER_SESSION_KEY = "gateway:creds:session:s-2:did:orcha:agent:other-99887766:TOKEN"


def _manifest(tags: list[str]) -> Response:
    return Response(
        200, json={"status": "success", "data": {"identity": {"id": DID, "tags": tags}}}
    )


class _Redis:
    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)
        self.patterns: list[str] = []
        self.deleted: list[str] = []

    async def sismember(self, _key: str, _member: str) -> bool:
        return False  # the JWT revocation set (require_auth)

    async def scan_iter(self, match: str):
        self.patterns.append(match)
        prefix, _, rest = match.partition("*")
        needle = rest.rstrip("*")
        for key in list(self.keys):
            if key.startswith(prefix) and needle in key:
                yield key

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        self.keys.remove(key)
        return 1


@pytest_asyncio.fixture
async def gw(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    monkeypatch.setattr(settings, "connections_enabled", True)
    calls: list[str] = []
    registry = MagicMock()
    registry.get = AsyncMock(return_value=_manifest(["mcp", "user", "connection"]))
    registry.delete = AsyncMock(return_value=Response(200, json={"status": "success"}))
    superagent = MagicMock()
    superagent.delete = AsyncMock(return_value=Response(200, json={"deleted": 1}))
    registry.get.side_effect = lambda *a, **k: (
        calls.append("registry.get"),
        registry.get.return_value,
    )[1]
    registry.delete.side_effect = lambda *a, **k: (
        calls.append("registry.delete"),
        registry.delete.return_value,
    )[1]
    superagent.delete.side_effect = lambda *a, **k: (
        calls.append("vault.delete"),
        superagent.delete.return_value,
    )[1]
    redis = _Redis(
        [
            f"gateway:creds:session:s-1:{DID}:MCP_TOKEN",
            f"gateway:creds:session:s-9:{DID}:OTHER",
            OTHER_SESSION_KEY,
        ]
    )
    app.state.registry = registry
    app.state.superagent = superagent
    app.state.redis = redis
    app.state.db = MagicMock()
    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, headers, registry, superagent, redis, calls


@pytest.mark.asyncio
async def test_revoke_removes_the_callers_token_then_deregisters(gw) -> None:
    ac, headers, registry, superagent, redis, calls = gw
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 204
    assert calls == ["registry.get", "vault.delete", "registry.delete"]
    # only the caller's rows, for this DID only
    superagent.delete.assert_awaited_once_with(
        f"/secrets/agent-env/{DID}", params={"user_id": "user-001"}
    )
    registry.delete.assert_awaited_once()
    assert registry.delete.await_args.args == (f"/api/v1/agents/{DID}",)
    assert (
        registry.delete.await_args.kwargs["headers"]["authorization"]
        == (headers["Authorization"])
    )
    # session copies of this connection's token are swept; nobody else's
    assert sorted(redis.deleted) == sorted(
        [
            f"gateway:creds:session:s-1:{DID}:MCP_TOKEN",
            f"gateway:creds:session:s-9:{DID}:OTHER",
        ]
    )
    assert redis.keys == [OTHER_SESSION_KEY]


@pytest.mark.asyncio
async def test_revoke_works_with_connections_turned_off(gw, monkeypatch) -> None:
    # Removing access is the safe direction: not gated on CONNECTIONS_ENABLED.
    from gateway.config import settings

    monkeypatch.setattr(settings, "connections_enabled", False)
    ac, headers, *_ = gw
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_an_agent_that_is_not_a_connection_is_not_removed_here(gw) -> None:
    ac, headers, registry, superagent, redis, _ = gw
    registry.get.return_value = _manifest(["mcp"])
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 404
    superagent.delete.assert_not_awaited()
    registry.delete.assert_not_awaited()
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_an_unknown_connection_is_404_and_nothing_is_removed(gw) -> None:
    ac, headers, registry, superagent, _, _ = gw
    registry.get.return_value = Response(404, json={"detail": "not found"})
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 404
    superagent.delete.assert_not_awaited()
    registry.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_id", ["did:orcha:system:web_search", "did:orcha:agent:x*y", "gh-mcp"]
)
async def test_an_id_that_is_not_a_connection_did_is_refused_unread(
    gw, agent_id
) -> None:
    ac, headers, registry, superagent, _, _ = gw
    resp = await ac.delete(f"/api/v1/plugins/mcp/{agent_id}", headers=headers)
    assert resp.status_code == 404
    registry.get.assert_not_awaited()
    superagent.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_vault_delete_stops_before_deregistering(gw) -> None:
    ac, headers, registry, superagent, redis, _ = gw
    superagent.delete.return_value = Response(500, text="db down")
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 502
    registry.delete.assert_not_awaited()
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_and_nothing_is_deregistered(gw) -> None:
    ac, headers, registry, superagent, redis, _ = gw
    registry.delete.return_value = Response(403, json={"detail": "not owner"})
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 403
    # the only rows touched were the caller's own (user_id is the caller)
    assert superagent.delete.await_args.kwargs["params"] == {"user_id": "user-001"}
    assert redis.deleted == []  # nothing swept for someone else's connection


@pytest.mark.asyncio
async def test_a_failed_deregistration_is_an_error_never_a_204(gw) -> None:
    ac, headers, registry, _, _, _ = gw
    registry.delete.return_value = Response(500, text="boom")
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 502
    assert "not deregistered" in resp.text


@pytest.mark.asyncio
async def test_a_failed_session_sweep_does_not_undo_the_revoke(gw) -> None:
    ac, headers, _, _, redis, _ = gw

    async def broken(match: str):
        raise RuntimeError("redis down")
        yield  # pragma: no cover

    redis.scan_iter = broken
    resp = await ac.delete(f"/api/v1/plugins/mcp/{DID}", headers=headers)
    assert resp.status_code == 204
