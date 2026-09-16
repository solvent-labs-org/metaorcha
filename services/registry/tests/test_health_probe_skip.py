"""SSE/HTTP MCP must skip the register-time GET /health probe."""

from __future__ import annotations

import pytest
import yaml

from services.registry.src.health_probe import should_skip_health_probe
from services.registry.src.models.emerge_config import EmergeConfig


def _cfg(**overrides: object) -> EmergeConfig:
    data = {
        "identity": {
            "id": "did:orcha:agent:docs-mcp",
            "name": "Docs MCP",
            "version": "1.0.0",
            "description": "test",
        },
        "protocol": {
            "type": "mcp",
            "version": "1.0",
            "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        },
        "health_endpoint": "https://example.com/mcp",
        "security": {"transport_layer": {"type": "none"}},
    }
    data.update(overrides)
    return EmergeConfig(**data)


def test_sse_mcp_skips_health():
    assert should_skip_health_probe(_cfg()) is True


def test_http_mcp_skips_health():
    cfg = _cfg(
        protocol={
            "type": "mcp",
            "version": "1.0",
            "transport": {"type": "http", "endpoint": "https://example.com/mcp"},
        }
    )
    assert should_skip_health_probe(cfg) is True


def test_stdio_still_skips():
    cfg = _cfg(
        protocol={
            "type": "mcp",
            "version": "1.0",
            "transport": {"type": "stdio", "command": "npx"},
        }
    )
    assert should_skip_health_probe(cfg) is True


def test_a2a_http_still_probes():
    cfg = _cfg(
        protocol={
            "type": "a2a",
            "version": "1.0",
            "transport": {"type": "http", "endpoint": "https://example.com/a2a"},
        },
        health_endpoint="https://example.com/health",
    )
    assert should_skip_health_probe(cfg) is False


@pytest.mark.asyncio
async def test_sse_mcp_skips_health_and_still_harvests():
    from unittest.mock import AsyncMock, MagicMock

    from services.registry.src.services.registration import RegistrationService

    cfg = _cfg()
    svc = RegistrationService(MagicMock())
    svc._parse_emerge_yaml = MagicMock(return_value=cfg)
    svc.validation_service.validate_emerge_config = MagicMock(return_value=(True, None))
    svc._purge_soft_deleted_agent = AsyncMock()
    svc._assert_agent_not_exists = AsyncMock()
    svc._verify_health_endpoint = AsyncMock()
    harvest = MagicMock()
    harvest.capabilities = ["tools/list"]
    svc._harvest_capabilities = AsyncMock(return_value=harvest)
    agent = MagicMock()
    agent.id = "did:orcha:agent:docs-mcp"
    agent.name = "Docs MCP"
    agent.version = "1.0.0"
    svc._save_agent_to_db = AsyncMock(return_value=agent)
    svc._create_version_snapshot = AsyncMock()
    svc._build_registration_response = MagicMock(return_value={"status": "success"})

    result = await svc.register_agent("yaml", "user-1")

    svc._verify_health_endpoint.assert_not_awaited()
    svc._harvest_capabilities.assert_awaited()
    assert result == {"status": "success"}


def test_fixture_yaml_round_trip():
    raw = yaml.safe_load(
        """
identity:
  id: did:orcha:agent:x
  name: X
  version: "1.0.0"
  description: d
protocol:
  type: mcp
  version: "1.0"
  transport:
    type: sse
    endpoint: https://example.com/mcp
health_endpoint: https://example.com/mcp
security:
  transport_layer:
    type: none
"""
    )
    assert should_skip_health_probe(EmergeConfig(**raw)) is True
