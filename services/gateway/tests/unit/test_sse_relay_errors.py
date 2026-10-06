"""The SSE relay never sends an exception's text to the browser.

CodeQL py/stack-trace-exposure: the relay's catch-all forwarded ``str(exc)``,
and a connection failure's text names internal hosts and addresses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.sessions.sse_relay import proxy_superagent_sse

INTERNAL = "10.0.0.7:8002"


async def _events(handler) -> list[dict]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://sa") as client:
        chunks = [
            c async for c in proxy_superagent_sse(client, "/sessions/s1/message", {})
        ]
    for chunk in chunks:
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    return [json.loads(c[6:]) for c in chunks]


@pytest.mark.asyncio
async def test_a_connection_failure_reaches_the_browser_without_its_detail(caplog):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect to {INTERNAL} refused", request=request)

    events = await _events(refuse)
    assert events == [{"type": "error", "error": "Upstream connection error"}]
    assert INTERNAL not in json.dumps(events)
    assert INTERNAL in caplog.text  # the operator still sees it


@pytest.mark.asyncio
async def test_an_upstream_status_error_names_only_the_status():
    events = await _events(lambda request: httpx.Response(502))
    assert events == [{"type": "error", "error": "Upstream error 502"}]


@pytest.mark.asyncio
async def test_a_good_stream_is_forwarded_unchanged():
    body = 'data: {"type": "token", "content": "hi"}\n\n: keep-alive\n\ndata: {"type": "done"}\n\n'
    events = await _events(lambda request: httpx.Response(200, text=body))
    assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]
