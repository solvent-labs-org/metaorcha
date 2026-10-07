"""Story 1.7: revoke a connection without breaking old receipts (FR-22, AD-6).

Real node → pipeline → PreFlight → handler path, with the Registry faked at
the HTTP client (so the manifest cache's real fetch, normalise and strict
paths run), the vault faked as rows keyed exactly as the real one keys them,
and the MCP handler recording every call — "no request reaches the platform"
is asserted as "the handler was not called and the health probe was not
sent".

- Deleting the token: the next call fails closed with ``credential_missing``
  — also when a session-scoped copy of the token is in the run config, and
  never by falling back to a bare row that is not this connection's.
- Removing the whole connection: the caller's rows go, nobody else's; the
  Registry says ``is_active: false`` and the next call fails with
  ``connection_revoked`` even though the manifest was cached a moment ago.
- A receipt sealed before the revoke still verifies with the published
  package, byte for byte, and holds no token bytes.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from emerge.run_attestation import canonical_json_bytes, verify_run_attestation
from langchain_core.messages import AIMessage
from superagent.api import routes as api_routes
from superagent.config import settings
from superagent.graph.state import SESSION_CREDENTIALS_CONFIG_KEY
from superagent.middleware import preflight as preflight_mod
from superagent.middleware.manifest_cache import ManifestCache, ManifestUnavailable
from superagent.middleware.observers import NoOpObserver, set_observer
from superagent.nodes import execute_agent_calls as node_mod
from superagent.nodes.execute_agent_calls import execute_agent_calls_node
from superagent.vault.client import VaultClient

CANARY = "canary-3e7d1f0b-revoked-token-never-leaks"
USER = "user-1"
OTHER_USER = "user-2"
SSE_DID = "did:orcha:agent:gh-1a2b3c4d"
STDIO_DID = "did:orcha:agent:local-9e8f7a6b"
PLAIN_DID = "did:orcha:agent:docs-5c6d7e8f"  # a connection with no token
NEIGHBOUR_DID = "did:orcha:agent:gh-1a2b3c4d5"  # the DID above is its prefix

BEARER = {"id": "b", "type": "http_bearer", "config": {"token_vault_ref": "GH_TOKEN"}}
REGISTRY_RECORDS: dict[str, dict[str, Any]] = {
    SSE_DID: {
        "name": "GitHub",
        "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        "auth_strategies": [BEARER],
    },
    STDIO_DID: {
        # env-only: the token reaches the subprocess as ${LOCAL_TOKEN} alone
        "name": "Local tools",
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "some-mcp"],
            "env": {"LOCAL_TOKEN": "${LOCAL_TOKEN}", "LOG_LEVEL": "info"},
        },
        "auth_strategies": [],
    },
    PLAIN_DID: {
        "name": "Public docs",
        "transport": {"type": "sse", "endpoint": "https://docs.example.com/mcp"},
        "auth_strategies": [],
    },
}
VAR = {SSE_DID: "GH_TOKEN", STDIO_DID: "LOCAL_TOKEN"}


def _key(agent_id: str, var: str) -> str:
    return f"agent:{agent_id}:env:{var}"


# -- fakes ------------------------------------------------------------------


class FakeRegistry:
    """The Registry's manifest GET, as JSON over a fake HTTP client."""

    def __init__(self) -> None:
        self.records = copy.deepcopy(REGISTRY_RECORDS)
        self.active = dict.fromkeys(self.records, True)
        self.down = False
        self.omit_is_active = False
        self.gets: list[str] = []

    async def get(self, path: str) -> httpx.Response:
        agent_id = path.rsplit("/", 1)[-1]
        self.gets.append(agent_id)
        request = httpx.Request("GET", f"http://registry{path}")
        if self.down:
            raise httpx.ConnectError("registry unreachable", request=request)
        rec = self.records.get(agent_id)
        if rec is None:
            return httpx.Response(404, json={"detail": "nf"}, request=request)
        metadata = {"health_status": "healthy", "health_endpoint": ""}
        if not self.omit_is_active:
            metadata["is_active"] = self.active[agent_id]
        body = {
            "status": "success",
            "data": {
                "identity": {
                    "id": agent_id,
                    "name": rec["name"],
                    "tags": ["mcp", "user", "connection"],
                },
                "metadata": metadata,
                "protocol": {"type": "mcp", "transport": rec["transport"]},
                "security": {"auth_strategies": rec["auth_strategies"]},
                "payment": {"enabled": False},
                "capabilities": [],
            },
        }
        return httpx.Response(200, json=body, request=request)


class RowVault:
    """The vault as rows keyed exactly like ``user_secrets`` (user_id, key)."""

    def __init__(self, rows: dict[tuple[str, str], str]) -> None:
        self.rows = dict(rows)

    async def get_agent_env(self, user_id: str, agent_id: str, var: str):
        return self.rows.get((user_id, _key(agent_id, var)))

    async def get_user_secret(self, user_id: str, key: str):
        return self.rows.get((user_id, key))

    async def delete_agent_env(self, user_id: str, agent_id: str, var: str) -> int:
        return int(self.rows.pop((user_id, _key(agent_id, var)), None) is not None)

    async def delete_all_agent_env(self, user_id: str, agent_id: str) -> int:
        prefix = f"agent:{agent_id}:env:"
        doomed = [k for k in self.rows if k[0] == user_id and k[1].startswith(prefix)]
        for k in doomed:
            del self.rows[k]
        return len(doomed)

    async def save_agent_env(self, *a: Any) -> None:
        raise AssertionError("a revoke never writes the vault")

    async def save_user_secret(self, *a: Any) -> None:
        raise AssertionError("a revoke never writes the vault")


@pytest.fixture(autouse=True)
def _observer():
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)
    registry = FakeRegistry()
    cache = ManifestCache()
    monkeypatch.setattr(cache, "_get_client", lambda: registry)
    monkeypatch.setattr(preflight_mod, "MANIFEST_CACHE", cache)
    # the pipeline's fee lookup reads the module-level singleton
    monkeypatch.setattr("superagent.middleware.manifest_cache.MANIFEST_CACHE", cache)
    vault = RowVault(
        {
            (USER, _key(SSE_DID, "GH_TOKEN")): CANARY,
            (USER, _key(STDIO_DID, "LOCAL_TOKEN")): CANARY,
            # rows a revoke must never touch
            (USER, "GH_TOKEN"): "bare-legacy-row",
            (USER, _key(NEIGHBOUR_DID, "GH_TOKEN")): "neighbour-token",
            (OTHER_USER, _key(SSE_DID, "GH_TOKEN")): "other-users-token",
        }
    )
    monkeypatch.setattr("superagent.vault.client.VaultClient", lambda: vault)
    healthy = AsyncMock()
    monkeypatch.setattr(preflight_mod.PreFlightManager, "_assert_healthy", healthy)
    monkeypatch.setattr(
        preflight_mod, "_redis_has_grant", AsyncMock(return_value=False)
    )
    sent: list[dict[str, Any]] = []

    async def call_tool(self, *, agent_id, capability_id, args, transport):
        sent.append(
            {
                "agent_id": agent_id,
                "headers": dict(self._auth_headers),
                "env": dict(transport.get("resolved_env") or {}),
            }
        )
        return {"content": [{"type": "text", "text": f"ok from {capability_id}"}]}

    monkeypatch.setattr(
        "superagent.handlers.mcp_handler.MCPHandler.call_tool", call_tool
    )
    return types.SimpleNamespace(
        registry=registry, cache=cache, vault=vault, healthy=healthy, sent=sent
    )


def _tool_name(agent_id: str, capability_id: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in agent_id)
    return f"{safe_id}__{capability_id}"


def _state(agent_id: str, capability: str, session: str) -> dict[str, Any]:
    return {
        "session_id": session,
        "user_id": USER,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": _tool_name(agent_id, capability),
                        "args": {"owner": "o", "repo": "r"},
                        "id": f"call-{session}",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "pnd_candidates": [
            {
                "agent_id": agent_id,
                "agent_name": REGISTRY_RECORDS[agent_id]["name"],
                "protocol_type": "MCP",
                "capabilities": [
                    {
                        "capability_id": capability,
                        "capability_type": "TOOL",
                        "description": "cap",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
        "_pending_events": [],
        "artifacts": {},
    }


async def _turn(
    agent_id: str,
    session: str,
    capability: str = "search_repos",
    session_credentials: dict[str, dict[str, str]] | None = None,
) -> str:
    config = {
        "configurable": {
            "thread_id": session,
            SESSION_CREDENTIALS_CONFIG_KEY: session_credentials or {},
        }
    }
    updates = await execute_agent_calls_node(
        _state(agent_id, capability, session), config
    )
    (msg,) = updates["messages"]
    return str(msg.content)


# -- AC1: delete the token --------------------------------------------------


@pytest.mark.parametrize("agent_id", [SSE_DID, STDIO_DID])
async def test_deleting_the_token_stops_the_next_call_before_any_request(
    world, agent_id
) -> None:
    assert (await _turn(agent_id, "s-before")).startswith("ok from")
    assert len(world.sent) == 1  # positive control: the call did go out

    await api_routes.delete_agent_env(agent_id, VAR[agent_id], user_id=USER)
    world.healthy.reset_mock()
    # A session-scoped copy of the same token is in the run config: it must
    # not revive the connection (the stdio env resolver reads it first).
    content = await _turn(
        agent_id,
        "s-after",
        session_credentials={agent_id: {VAR[agent_id]: CANARY}},
    )
    assert "credential_missing" in content and VAR[agent_id] in content
    assert len(world.sent) == 1  # nothing further reached the platform
    world.healthy.assert_not_awaited()  # not even the health probe


async def test_a_bare_row_never_keeps_a_connection_callable(world) -> None:
    # Only the bare legacy ``GH_TOKEN`` row is left: it is not this
    # connection's token (it may be another agent's), so it is never sent.
    del world.vault.rows[(USER, _key(SSE_DID, "GH_TOKEN"))]
    content = await _turn(SSE_DID, "s-bare")
    assert "credential_missing" in content
    assert world.sent == []


async def test_a_token_removed_mid_call_never_falls_back_to_a_bare_row(
    world, monkeypatch
) -> None:
    # The connection check reads the token, then it is revoked before the
    # header is resolved: the header read must not fall back to the bare row.
    reads = {"n": 0}
    real = world.vault.get_agent_env

    async def racing(user_id, agent_id, var):
        reads["n"] += 1
        return await real(user_id, agent_id, var) if reads["n"] == 1 else None

    monkeypatch.setattr(world.vault, "get_agent_env", racing)
    content = await _turn(SSE_DID, "s-race")
    assert "credential_missing" in content
    assert world.sent == []


# -- AC2: remove the whole connection ---------------------------------------


@pytest.mark.parametrize("agent_id", [SSE_DID, PLAIN_DID])
async def test_removing_the_connection_stops_it_even_while_cached(
    world, agent_id
) -> None:
    assert (await _turn(agent_id, "s-before")).startswith("ok from")
    assert agent_id in world.cache._cache  # the manifest is cached now

    await api_routes.delete_all_agent_env(agent_id, user_id=USER)
    world.registry.active[agent_id] = False  # the Registry's soft delete
    world.healthy.reset_mock()
    content = await _turn(agent_id, "s-after")
    assert "connection_revoked" in content
    assert len(world.sent) == 1
    world.healthy.assert_not_awaited()


async def test_removing_a_connection_leaves_every_other_row(world) -> None:
    deleted = await api_routes.delete_all_agent_env(SSE_DID, user_id=USER)
    assert deleted == {"deleted": 1}
    assert (USER, _key(SSE_DID, "GH_TOKEN")) not in world.vault.rows
    assert world.vault.rows == {
        (USER, _key(STDIO_DID, "LOCAL_TOKEN")): CANARY,
        (USER, "GH_TOKEN"): "bare-legacy-row",
        (USER, _key(NEIGHBOUR_DID, "GH_TOKEN")): "neighbour-token",
        (OTHER_USER, _key(SSE_DID, "GH_TOKEN")): "other-users-token",
    }
    # and the other connection still works
    assert (await _turn(STDIO_DID, "s-other")).startswith("ok from")


async def test_an_unreadable_registry_fails_a_connection_closed(world) -> None:
    assert (await _turn(SSE_DID, "s-before")).startswith("ok from")
    world.registry.down = True  # the cached copy must not be trusted
    content = await _turn(SSE_DID, "s-down")
    assert "connection_unavailable" in content
    assert len(world.sent) == 1


async def test_a_registry_that_does_not_say_active_fails_closed(world) -> None:
    world.registry.omit_is_active = True
    content = await _turn(SSE_DID, "s-old-registry")
    assert "connection_unavailable" in content
    assert world.sent == []


async def test_revoking_while_an_approval_card_is_open(world, monkeypatch) -> None:
    # 1.5: a write pauses for a human. The user removes the connection while
    # the card is open, then approves: the resumed call is refused.
    prompts: list[dict[str, Any]] = []

    def approve_after_revoking(event: dict[str, Any]) -> dict[str, Any]:
        prompts.append(event)
        world.registry.active[SSE_DID] = False
        return {"status": "approved", "authoriser_user_id": USER}

    monkeypatch.setattr(node_mod, "interrupt", approve_after_revoking)
    recorded: list[Any] = []

    class Recorder:
        async def on_step_complete(self, record: Any) -> None:
            recorded.append(record)

    set_observer(Recorder())
    content = await _turn(SSE_DID, "s-card", capability="create_issue")
    assert len(prompts) == 1 and prompts[0]["interrupt_type"] == "HITL_APPROVAL"
    assert "connection_revoked" in content
    assert world.sent == []
    # the approval is never recorded as a pass for a call that did not run
    assert not any(
        (r.metadata or {}).get("scope_approval", {}).get("result") == "pass"
        for r in recorded
    )


# -- the manifest cache seam --------------------------------------------------


async def test_a_fresh_read_is_strict_and_the_cached_read_is_not() -> None:
    cache = ManifestCache()
    registry = FakeRegistry()
    registry.down = True
    cache._get_client = lambda: registry  # type: ignore[method-assign]
    fallback = await cache.get_manifest(SSE_DID)
    assert fallback.get("tags") is None  # the old, untagged fallback
    with pytest.raises(ManifestUnavailable):
        await cache.get_manifest(SSE_DID, fresh=True)


def test_is_active_is_carried_only_when_the_registry_sends_it() -> None:
    raw = {"data": {"identity": {"id": SSE_DID}, "metadata": {"is_active": False}}}
    assert ManifestCache._normalise(raw)["is_active"] is False
    assert "is_active" not in ManifestCache._normalise({"data": {"metadata": {}}})


# -- the real vault client's deletes -----------------------------------------


class _FakeUserSecret:
    def __init__(self, rows: list[dict[str, str]], fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.wheres: list[dict[str, Any]] = []

    @staticmethod
    def _match(row: dict[str, str], where: dict[str, Any]) -> bool:
        for field, cond in where.items():
            value = row[field]
            if isinstance(cond, dict):
                if not value.startswith(cond["startswith"]):
                    return False
            elif value != cond:
                return False
        return True

    async def delete_many(self, where: dict[str, Any]) -> int:
        self.wheres.append(where)
        if self.fail:
            raise RuntimeError("database is down")
        doomed = [r for r in self.rows if self._match(r, where)]
        for r in doomed:
            self.rows.remove(r)
        return len(doomed)


def _prisma(monkeypatch, table: _FakeUserSecret) -> None:
    class Prisma:
        usersecret = table

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    module = types.ModuleType("src.generated_client")
    module.Prisma = Prisma  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.generated_client", module)


async def test_the_vault_deletes_one_connections_rows_for_one_user(
    monkeypatch,
) -> None:
    rows = [
        {"user_id": USER, "key": _key(SSE_DID, "GH_TOKEN")},
        {"user_id": USER, "key": _key(SSE_DID, "EXTRA")},
        {"user_id": USER, "key": _key(NEIGHBOUR_DID, "GH_TOKEN")},
        {"user_id": USER, "key": "GH_TOKEN"},
        {"user_id": OTHER_USER, "key": _key(SSE_DID, "GH_TOKEN")},
    ]
    table = _FakeUserSecret(rows)
    _prisma(monkeypatch, table)
    assert await VaultClient().delete_all_agent_env(USER, SSE_DID) == 2
    assert table.wheres == [
        {"user_id": USER, "key": {"startswith": f"agent:{SSE_DID}:env:"}}
    ]
    assert rows == [
        {"user_id": USER, "key": _key(NEIGHBOUR_DID, "GH_TOKEN")},
        {"user_id": USER, "key": "GH_TOKEN"},
        {"user_id": OTHER_USER, "key": _key(SSE_DID, "GH_TOKEN")},
    ]


async def test_a_missing_row_is_zero_and_a_database_error_propagates(
    monkeypatch,
) -> None:
    table = _FakeUserSecret([])
    _prisma(monkeypatch, table)
    assert await VaultClient().delete_agent_env(USER, SSE_DID, "GH_TOKEN") == 0
    table.fail = True
    # The old delete swallowed every error: a revoke that did not happen
    # returned 204. Now the route fails and the Gateway reports it.
    with pytest.raises(RuntimeError, match="database is down"):
        await VaultClient().delete_agent_env(USER, SSE_DID, "GH_TOKEN")
    with pytest.raises(RuntimeError, match="database is down"):
        await VaultClient().delete_all_agent_env(USER, SSE_DID)


async def test_a_failed_delete_logs_the_request_ids_on_one_line(
    monkeypatch, caplog
) -> None:
    # user and agent ids come from the request: a line break in either must
    # not start a second, forged log line
    table = _FakeUserSecret([])
    table.fail = True
    _prisma(monkeypatch, table)
    with (
        caplog.at_level(logging.ERROR, logger="superagent.vault.client"),
        pytest.raises(RuntimeError, match="database is down"),
    ):
        await VaultClient().delete_all_agent_env("user-1\nERROR forged", SSE_DID)
    (message,) = [
        r.getMessage() for r in caplog.records if r.name == "superagent.vault.client"
    ]
    assert "\n" not in message and "\r" not in message
    assert "user-1\\nERROR forged" in message


# -- AC3: a receipt sealed before the revoke --------------------------------


class _AttestationTable:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def create(self, data: dict[str, Any]) -> Any:
        self.rows.append(dict(data))
        return types.SimpleNamespace(id=f"att-{len(self.rows)}", **data)


async def test_a_receipt_sealed_before_the_revoke_still_verifies(world) -> None:
    from validator.run_observer import RunAttestationObserver

    table = _AttestationTable()
    observer = RunAttestationObserver(db=types.SimpleNamespace(attestation=table))
    set_observer(observer)
    assert (await _turn(SSE_DID, "s-sealed")).startswith("ok from")
    assert world.sent[0]["headers"] == {"Authorization": f"Bearer {CANARY}"}
    await observer.on_run_complete("s-sealed")
    (row,) = table.rows
    payload = row["payload"]
    sealed = json.loads(json.dumps(getattr(payload, "data", payload)))
    before = canonical_json_bytes(sealed)

    # revoke the whole connection, then try to use it
    await api_routes.delete_all_agent_env(SSE_DID, user_id=USER)
    world.registry.active[SSE_DID] = False
    set_observer(NoOpObserver())
    assert "connection_revoked" in await _turn(SSE_DID, "s-after")

    # the stored receipt is untouched and verifies with the published package
    (row_after,) = table.rows
    payload_after = row_after["payload"]
    envelope = json.loads(json.dumps(getattr(payload_after, "data", payload_after)))
    assert canonical_json_bytes(envelope) == before
    assert verify_run_attestation(envelope).valid
    assert [s["tool"] for s in envelope["steps"]] == [f"{SSE_DID}#search_repos"]
    assert CANARY not in before.decode()
