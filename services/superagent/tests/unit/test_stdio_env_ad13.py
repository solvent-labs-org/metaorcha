"""AD-13: vault-only ``${VAR}``, allow-listed subprocess env, platform_env for system DIDs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from superagent.handlers.mcp_handler import STDIO_ENV_ALLOWLIST, build_stdio_env
from superagent.middleware.preflight import (
    PreFlightError,
    PreFlightManager,
    platform_env_allowed,
)

# ── subprocess environment ───────────────────────────────────────────────────


def test_stdio_env_is_allowlisted_host_plus_connection_vars():
    host = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "VAULT_KEY": "service-secret",
        "ATTESTATION_PRIVATE_KEY_B64": "signing-secret",
        "DATABASE_URL": "postgres://",
        "OPENROUTER_API_KEY": "llm-secret",
    }
    env = build_stdio_env({"GITHUB_TOKEN": "from-vault"}, host_env=host)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "GITHUB_TOKEN": "from-vault",
    }
    for leaked in ("VAULT_KEY", "ATTESTATION_PRIVATE_KEY_B64", "DATABASE_URL"):
        assert leaked not in env


def test_stdio_env_allowlist_is_exactly_path_home_locale():
    assert set(STDIO_ENV_ALLOWLIST) == {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE"}


def test_stdio_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", "service-secret")
    monkeypatch.setenv("PATH", "/bin")
    env = build_stdio_env(None)
    assert env["PATH"] == "/bin"
    assert "VAULT_KEY" not in env


# ── ${VAR} resolution: vault only ────────────────────────────────────────────


def _manager(vault_value: str | None) -> PreFlightManager:
    vault = MagicMock()
    vault.get_agent_env = AsyncMock(return_value=vault_value)
    return PreFlightManager(vault)


async def test_unresolved_var_fails_closed_even_when_host_has_it(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "host-value-must-never-be-used")
    pm = _manager(None)
    with pytest.raises(PreFlightError, match=r"^env_unresolved:"):
        await pm._resolve_stdio_env(
            agent_id="did:orcha:agent:gh",
            user_id="user-1",
            raw_env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            session_credentials=None,
        )


async def test_vault_value_resolves(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "host-value-must-never-be-used")
    pm = _manager("vault-value")
    resolved = await pm._resolve_stdio_env(
        agent_id="did:orcha:agent:gh",
        user_id="user-1",
        raw_env={"GITHUB_TOKEN": "${GITHUB_TOKEN}", "STATIC": "plain"},
        session_credentials=None,
    )
    assert resolved == {"GITHUB_TOKEN": "vault-value", "STATIC": "plain"}
    pm._vault.get_agent_env.assert_awaited_once_with(
        "user-1", "did:orcha:agent:gh", "GITHUB_TOKEN"
    )


async def test_session_credentials_still_win():
    pm = _manager(None)
    resolved = await pm._resolve_stdio_env(
        agent_id="did:orcha:agent:gh",
        user_id="user-1",
        raw_env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
        session_credentials={"did:orcha:agent:gh": {"GITHUB_TOKEN": "session"}},
    )
    assert resolved == {"GITHUB_TOKEN": "session"}


# ── platform_env is a platform-tool mechanism ────────────────────────────────


def test_platform_env_only_for_system_dids():
    assert platform_env_allowed("did:orcha:system:web-search")
    assert not platform_env_allowed("did:orcha:agent:local-mcp")
    assert not platform_env_allowed("")
    assert not platform_env_allowed(None)  # type: ignore[arg-type]
