"""Story 1.7: removing an agent stops it without erasing what it did.

The Registry's delete is a soft delete (``is_active = false``): the row, its
capabilities and its transport stay, so an old run that names the DID still
resolves. The manifest GET keeps answering for a removed agent and says so
with ``metadata.is_active`` — a 404 would reach the SuperAgent as an empty,
untagged manifest, and a connection would silently read as an ordinary agent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from services.registry.src.api.v1.agents import delete_agent, get_agent_manifest

DID = "did:orcha:agent:docs-mcp-0a1b2c3d"


def _agent(*, is_active: bool, owner: str = "user-001") -> SimpleNamespace:
    return SimpleNamespace(
        id=DID,
        user_id=owner,
        name="Docs MCP",
        version="1.0.0",
        provider=None,
        owner_contact=None,
        description="User MCP: Docs MCP",
        tags=["mcp", "user", "connection"],
        indexed_at=datetime(2026, 9, 30, tzinfo=UTC),
        health_status="HEALTHY",
        health_endpoint="https://example.com/mcp",
        protocol_type="MCP",
        protocol_version="1.0",
        transport=SimpleNamespace(
            type="SSE",
            endpoint="https://example.com/mcp",
            command=None,
            args=None,
            env=None,
        ),
        security=None,
        payment=None,
        authorized_scope=None,
        capabilities=[],
        is_active=is_active,
    )


def _db(agent: SimpleNamespace | None) -> MagicMock:
    db = MagicMock()
    db.agent.find_unique = AsyncMock(return_value=agent)
    db.agent.update = AsyncMock()
    db.agent.delete = AsyncMock()
    db.agent.delete_many = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("is_active", [True, False])
async def test_the_manifest_says_whether_the_agent_is_active(is_active) -> None:
    body = await get_agent_manifest(DID, "user-001", _db(_agent(is_active=is_active)))
    assert body["data"]["metadata"]["is_active"] is is_active
    # a removed agent still resolves: same identity, same tags
    assert body["data"]["identity"]["id"] == DID
    assert "connection" in body["data"]["identity"]["tags"]


@pytest.mark.asyncio
async def test_delete_is_soft_and_touches_only_the_flag() -> None:
    db = _db(_agent(is_active=True))
    resp = await delete_agent(DID, "user-001", db)
    assert resp.status == "success"
    db.agent.update.assert_awaited_once_with(
        where={"id": DID}, data={"is_active": False}
    )
    db.agent.delete.assert_not_called()
    db.agent.delete_many.assert_not_called()
    touched = {name.split(".")[0] for name, _, _ in db.mock_calls}
    assert touched == {"agent"}  # no other table is written


@pytest.mark.asyncio
async def test_a_non_owner_cannot_remove_it_and_nothing_changes() -> None:
    db = _db(_agent(is_active=True, owner="user-001"))
    with pytest.raises(HTTPException) as exc:
        await delete_agent(DID, "user-002", db)
    assert exc.value.status_code == 403
    db.agent.update.assert_not_called()
