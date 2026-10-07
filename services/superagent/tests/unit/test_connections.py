"""Story 1.3: a connection is callable only when on, and only with its token.

A connection is an agent the Gateway connect route registered (manifest tag
``connection``). Its token lives in the caller's own vault under
``agent:<DID>:env:<VAR>`` (AD-15). With CONNECTIONS_ENABLED off, or with no
token, the call fails with a named error before any request reaches the
platform. Agents registered any other way keep today's behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from superagent.config import settings
from superagent.middleware.auth_manager import (
    AuthManager,
    AuthResolutionError,
    read_credential,
    token_ref,
)
from superagent.middleware.manifest_cache import ManifestCache
from superagent.middleware.preflight import (
    AuthInterruptRequired,
    PreFlightError,
    PreFlightManager,
)

DID = "did:orcha:agent:github-1a2b3c4d"
BEARER = {
    "id": "b",
    "type": "http_bearer",
    "config": {"token_vault_ref": "GITHUB_TOKEN"},
}


def _vault(scoped: str | None = None, bare: str | None = None) -> MagicMock:
    vault = MagicMock()
    vault.get_agent_env = AsyncMock(return_value=scoped)
    vault.get_user_secret = AsyncMock(return_value=bare)
    vault.save_user_secret = AsyncMock()
    vault.save_agent_env = AsyncMock()
    vault.delete_agent_env = AsyncMock()
    return vault


def _manifest(*, tags: list[str], strategies: list[dict]) -> dict:
    return {
        "agent_id": DID,
        "tags": tags,
        "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        "security": {"auth_strategies": strategies},
        "capabilities": [],
    }


@pytest.fixture
def connections_on(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)


# -- the vault read (AD-15) -------------------------------------------------


async def test_the_scoped_key_is_read_first() -> None:
    vault = _vault(scoped="tok-scoped", bare="tok-bare")
    assert await read_credential(vault, "u1", DID, "GITHUB_TOKEN") == "tok-scoped"
    vault.get_agent_env.assert_awaited_once_with("u1", DID, "GITHUB_TOKEN")
    vault.get_user_secret.assert_not_awaited()


async def test_a_bare_legacy_row_is_read_but_never_copied_or_deleted() -> None:
    vault = _vault(scoped=None, bare="tok-bare")
    assert await read_credential(vault, "u1", DID, "GITHUB_TOKEN") == "tok-bare"
    vault.save_user_secret.assert_not_awaited()
    vault.save_agent_env.assert_not_awaited()
    vault.delete_agent_env.assert_not_awaited()


def test_token_ref_names_the_variable_of_token_strategies_only() -> None:
    assert token_ref(BEARER) == "GITHUB_TOKEN"
    assert (
        token_ref({"type": "X_API_KEY", "config": {"key_vault_ref": "API_KEY"}})
        == "API_KEY"
    )
    assert token_ref({"type": "oauth2", "config": {"token_vault_ref": "X"}}) is None
    assert token_ref({"type": "http_bearer", "config": "bad"}) is None


# -- the Registry's lower-case strategy types --------------------------------


async def test_a_lower_case_http_bearer_resolves_the_scoped_token() -> None:
    vault = _vault(scoped="tok-scoped")
    headers = await AuthManager(vault).resolve(DID, "u1", [BEARER])
    assert headers == {"Authorization": "Bearer tok-scoped"}


async def test_a_lower_case_x_api_key_resolves_with_its_header() -> None:
    vault = _vault(scoped="k-1")
    strategy = {
        "type": "x_api_key",
        "config": {"key_vault_ref": "API_KEY", "header_name": "X-Key"},
    }
    assert await AuthManager(vault).resolve(DID, "u1", [strategy]) == {"X-Key": "k-1"}


async def test_lower_case_oauth_keeps_its_old_behaviour() -> None:
    vault = _vault()
    manager = AuthManager(vault)
    with (
        patch.object(manager, "_oauth2", AsyncMock()) as oauth2,
        pytest.raises(AuthResolutionError),
    ):
        await manager.resolve(DID, "u1", [{"type": "oauth2", "config": {}}])
    oauth2.assert_not_awaited()


# -- the manifest carries its tags --------------------------------------------


def test_the_manifest_cache_keeps_identity_tags() -> None:
    raw = {"data": {"identity": {"id": DID, "tags": ["mcp", "user", "connection"]}}}
    assert ManifestCache._normalise(raw)["tags"] == ["mcp", "user", "connection"]
    assert ManifestCache._normalise({"data": {"identity": {"id": DID}}})["tags"] == []


# -- preflight --------------------------------------------------------------


async def _preflight(manifest: dict, vault: MagicMock):
    manager = PreFlightManager(vault)
    healthy = AsyncMock()
    with (
        patch(
            "superagent.middleware.preflight.MANIFEST_CACHE.get_manifest",
            AsyncMock(return_value=manifest),
        ),
        patch.object(manager, "_assert_healthy", healthy),
        patch(
            "superagent.middleware.preflight._redis_has_grant",
            AsyncMock(return_value=False),
        ),
    ):
        try:
            result = await manager.run(
                agent_id=DID,
                user_id="u1",
                capability_id="search_repos",
                tool_name="gh__search_repos",
                state={"session_id": "s1"},
            )
        finally:
            probed = healthy.await_count
    return result, probed


async def test_a_connection_is_refused_while_connections_are_off() -> None:
    manifest = _manifest(tags=["mcp", "user", "connection"], strategies=[BEARER])
    manager = PreFlightManager(_vault(scoped="tok"))
    healthy = AsyncMock()
    with (
        patch(
            "superagent.middleware.preflight.MANIFEST_CACHE.get_manifest",
            AsyncMock(return_value=manifest),
        ),
        patch.object(manager, "_assert_healthy", healthy),
        pytest.raises(PreFlightError, match="^connections_disabled"),
    ):
        await manager.run(
            agent_id=DID,
            user_id="u1",
            capability_id="search_repos",
            tool_name="gh__search_repos",
            state={},
        )
    healthy.assert_not_awaited()  # nothing was sent to the platform


async def test_a_missing_token_fails_closed_before_any_request(connections_on) -> None:
    manifest = _manifest(tags=["mcp", "user", "connection"], strategies=[BEARER])
    manager = PreFlightManager(_vault())
    healthy = AsyncMock()
    with (
        patch(
            "superagent.middleware.preflight.MANIFEST_CACHE.get_manifest",
            AsyncMock(return_value=manifest),
        ),
        patch.object(manager, "_assert_healthy", healthy),
        pytest.raises(PreFlightError, match="^credential_missing: .*GITHUB_TOKEN"),
    ):
        await manager.run(
            agent_id=DID,
            user_id="u1",
            capability_id="search_repos",
            tool_name="gh__search_repos",
            state={},
        )
    healthy.assert_not_awaited()


async def test_a_connection_with_its_token_resolves_the_bearer(connections_on) -> None:
    manifest = _manifest(tags=["mcp", "user", "connection"], strategies=[BEARER])
    result, probed = await _preflight(manifest, _vault(scoped="tok-scoped"))
    assert result["headers"] == {"Authorization": "Bearer tok-scoped"}
    assert probed == 1


async def test_a_tokenless_connection_needs_no_credential(connections_on) -> None:
    manifest = _manifest(tags=["mcp", "user", "connection"], strategies=[])
    vault = _vault()
    result, _ = await _preflight(manifest, vault)
    assert result["headers"] == {}
    vault.get_agent_env.assert_not_awaited()


async def test_an_agent_that_is_not_a_connection_keeps_the_auth_interrupt() -> None:
    # CONNECTIONS_ENABLED is off and the agent has no token: an ordinary
    # registered agent still asks for the credential, exactly as before.
    manifest = _manifest(tags=["mcp"], strategies=[BEARER])
    manager = PreFlightManager(_vault())
    with (
        patch(
            "superagent.middleware.preflight.MANIFEST_CACHE.get_manifest",
            AsyncMock(return_value=manifest),
        ),
        patch.object(manager, "_assert_healthy", AsyncMock()),
        patch(
            "superagent.middleware.preflight._redis_has_grant",
            AsyncMock(return_value=False),
        ),
        patch.object(
            manager, "_build_auth_interrupt_event", AsyncMock(return_value=MagicMock())
        ),
        pytest.raises(AuthInterruptRequired),
    ):
        await manager.run(
            agent_id=DID,
            user_id="u1",
            capability_id="search_repos",
            tool_name="gh__search_repos",
            state={"session_id": "s1"},
        )
