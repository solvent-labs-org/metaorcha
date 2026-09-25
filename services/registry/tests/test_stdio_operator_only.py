"""AD-13: the Registry door refuses a stdio manifest from a non-operator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.registry.src.services.registration import RegistrationService

_STDIO_YAML = """
identity:
  id: "did:orcha:agent:local-mcp"
  name: "Local MCP"
  version: "1.0.0"
  description: "test"
protocol:
  type: mcp
  version: "1.0"
  transport:
    type: stdio
    command: "npx"
health_endpoint: "http://127.0.0.1:9/health"
security:
  transport_layer:
    type: none
payment:
  enabled: false
"""

_SSE_YAML = _STDIO_YAML.replace(
    'type: stdio\n    command: "npx"',
    'type: sse\n    endpoint: "https://example.com/mcp"',
).replace('"http://127.0.0.1:9/health"', '"https://example.com/mcp"')


class _Reached(Exception):
    """Raised by the mocked next step to prove the operator gate let us through."""


def _service() -> RegistrationService:
    svc = RegistrationService(MagicMock())
    # The gate must fire before any DB step: the first DB-touching call is the
    # soft-delete purge, then the existence check — both mocked; the second
    # raises so a passed gate is observable.
    svc._purge_soft_deleted_agent = AsyncMock()  # type: ignore[method-assign]
    svc._assert_agent_not_exists = AsyncMock(side_effect=_Reached())  # type: ignore[method-assign]
    return svc


def _no_operators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATOR_USER_IDS", raising=False)
    monkeypatch.delenv("DISABLE_AUTH", raising=False)


@pytest.mark.asyncio
async def test_non_operator_cannot_register_stdio(monkeypatch):
    _no_operators(monkeypatch)
    with pytest.raises(PermissionError, match="stdio_operator_only"):
        await _service().register_agent(
            emerge_yaml_content=_STDIO_YAML, user_id="user-1"
        )


@pytest.mark.asyncio
async def test_operator_passes_the_stdio_gate(monkeypatch):
    _no_operators(monkeypatch)
    monkeypatch.setenv("OPERATOR_USER_IDS", "user-1")
    with pytest.raises(_Reached):
        await _service().register_agent(
            emerge_yaml_content=_STDIO_YAML, user_id="user-1"
        )


@pytest.mark.asyncio
async def test_disable_auth_passes_the_stdio_gate(monkeypatch):
    _no_operators(monkeypatch)
    monkeypatch.setenv("DISABLE_AUTH", "true")
    with pytest.raises(_Reached):
        await _service().register_agent(
            emerge_yaml_content=_STDIO_YAML, user_id="dev_user"
        )


@pytest.mark.asyncio
async def test_sse_is_not_gated(monkeypatch):
    _no_operators(monkeypatch)
    with pytest.raises(_Reached):
        await _service().register_agent(emerge_yaml_content=_SSE_YAML, user_id="user-1")


@pytest.mark.asyncio
async def test_update_agent_has_the_same_gate(monkeypatch):
    _no_operators(monkeypatch)
    with pytest.raises(PermissionError, match="stdio_operator_only"):
        await _service().update_agent(
            agent_id="did:orcha:agent:local-mcp",
            emerge_yaml_content=_STDIO_YAML,
            user_id="user-1",
        )
