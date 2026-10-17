"""Story 1.3: a token-gated MCP server can be harvested, and the token stays put.

A connection's server may list its tools only to a token holder. The Gateway
sends the token in ``X-Harvest-Authorization``; the Registry uses it as the
``Authorization`` of the listing calls of that one harvest and stores, logs
and echoes nothing. The x402 dry-run ``tools/call`` never carries it: a call
made with the user's token can act on their account.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from services.registry.src.adapters.mcp import MCPAdapter
from services.registry.src.api.v1 import agents as agents_api
from services.registry.src.services.registration import RegistrationService
from services.registry.src.services.validation import ValidationError

TOKEN = "tok-not-a-real-secret-4f9a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_real_async_client = httpx.AsyncClient


def _client_factory(seen: list[dict[str, str]], status: int = 200):
    """httpx.AsyncClient stand-in that records each request's headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if status != 200:
            return httpx.Response(status, text="unauthorized")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _real_async_client(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_http_harvest_sends_the_token_on_every_listing_call() -> None:
    seen: list[dict[str, str]] = []
    adapter = MCPAdapter("https://example.com/mcp", headers=AUTH, max_retries=1)
    with patch(
        "services.registry.src.adapters.mcp.httpx.AsyncClient", _client_factory(seen)
    ):
        result = await adapter.harvest()
    assert result.errors == []
    assert len(seen) == 3  # tools/list, resources/list, prompts/list
    assert all(h.get("authorization") == AUTH["Authorization"] for h in seen)


@pytest.mark.asyncio
async def test_sse_harvest_hands_the_token_to_the_sse_client() -> None:
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, **_kwargs):
        captured["url"], captured["headers"] = url, headers
        raise ConnectionError("stop after the handshake request")
        yield  # pragma: no cover

    adapter = MCPAdapter("https://example.com/sse", transport_type="sse", headers=AUTH)
    with (
        patch("mcp.client.sse.sse_client", fake_sse_client),
        pytest.raises(ConnectionError),
    ):
        await adapter.harvest()
    assert captured["headers"] == AUTH


@pytest.mark.asyncio
async def test_the_x402_probe_never_carries_the_token() -> None:
    seen: list[dict[str, str]] = []
    adapter = MCPAdapter("https://example.com/mcp", headers=AUTH)
    with patch(
        "services.registry.src.adapters.mcp.httpx.AsyncClient", _client_factory(seen)
    ):
        await adapter._extract_x402("delete_repo", {})
    assert seen, "the probe made its request"
    assert all("authorization" not in h for h in seen)


@pytest.mark.asyncio
async def test_a_refused_harvest_leaks_the_token_nowhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[dict[str, str]] = []
    adapter = MCPAdapter("https://example.com/mcp", headers=AUTH, max_retries=1)
    with (
        caplog.at_level(logging.DEBUG),
        patch(
            "services.registry.src.adapters.mcp.httpx.AsyncClient",
            _client_factory(seen, status=401),
        ),
    ):
        result = await adapter.harvest()
    assert seen and all(h.get("authorization") for h in seen)
    assert result.capabilities == []
    assert result.errors  # the 401s are reported ...
    assert all(TOKEN not in e for e in result.errors)  # ... without the token
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_registration_passes_the_headers_to_the_adapter() -> None:
    from services.registry.src.models.emerge_config import EmergeConfig

    cfg = EmergeConfig(
        identity={
            "id": "did:orcha:agent:gh-1a2b3c4d",
            "name": "gh",
            "version": "1.0.0",
            "description": "d",
        },
        protocol={
            "type": "mcp",
            "version": "1.0",
            "transport": {"type": "sse", "endpoint": "https://example.com/sse"},
        },
        health_endpoint="https://example.com/sse",
        security={"transport_layer": {"type": "none"}},
    )
    svc = RegistrationService(MagicMock())
    adapter = MagicMock()
    adapter.harvest = AsyncMock(return_value=SimpleNamespace(capabilities=[]))
    with patch(
        "services.registry.src.services.registration.MCPAdapter",
        return_value=adapter,
    ) as cls:
        await svc._harvest_capabilities(cfg, "", AUTH)
    assert cls.call_args.kwargs["headers"] == AUTH


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}", None])
@pytest.mark.asyncio
async def test_the_register_route_reads_the_harvest_header(header) -> None:
    captured: dict[str, object] = {}

    async def fake_register(**kwargs):
        captured.update(kwargs)
        raise ValidationError("identity", "stop here")

    upload = MagicMock()
    upload.read = AsyncMock(return_value=b"identity: {}")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    service = MagicMock()
    service.register_agent = fake_register
    with (
        patch.object(agents_api, "RegistrationService", return_value=service),
        pytest.raises(HTTPException),
    ):
        await agents_api.register_agent(
            request, upload, "user-1", MagicMock(), harvest_authorization=header
        )
    expected = {"Authorization": header} if header else None
    assert captured["harvest_headers"] == expected
