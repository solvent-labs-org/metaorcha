"""End-to-end-ish tests for the emerge SDK: decorator → manifest → A2A server."""

from __future__ import annotations

import json
import urllib.request

import pytest

import emerge
from emerge.manifest import build_manifest
from emerge.sdk import clear_registry, registered_agents
from emerge.server import agent_card, serve_agent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def test_decorator_registers_agent_with_default_did():
    @emerge.agent(name="My Agent", description="does things")
    def handle(task: str) -> str:
        return task

    agents = registered_agents()
    assert len(agents) == 1
    spec = agents[0]
    assert spec.did == "did:orcha:agent:my-agent"
    assert spec.name == "My Agent"
    # No explicit skills → one derived skill so the agent card is never empty.
    assert spec.skills and spec.skills[0].id == "my-agent"


def test_decorator_returns_callable_unchanged():
    @emerge.agent(name="X", description="d")
    def handle(task: str) -> str:
        return "ok:" + task

    assert handle("hi") == "ok:hi"


def test_manifest_shape_matches_registry_schema():
    @emerge.agent(name="Priced", description="d", base_fee="0.10", port=8901)
    def handle(task: str) -> str:
        return task

    m = build_manifest(registered_agents()[0])
    assert m["schema_version"] == "1.0"
    assert m["identity"]["id"] == "did:orcha:agent:priced"
    assert m["protocol"]["type"] == "a2a"
    assert m["protocol"]["transport"]["endpoint"] == "http://localhost:8901"
    assert m["health_endpoint"] == "http://localhost:8901/health"
    assert m["payment"] == {"enabled": True, "base_fee": "0.10"}


def test_agent_card_is_valid():
    @emerge.agent(name="Carded", description="d", port=8902)
    def handle(task: str) -> str:
        return task

    card = agent_card(registered_agents()[0])
    assert card["name"] == "Carded"
    assert card["skills"]
    assert card["url"] == "http://localhost:8902"


def _post_jsonrpc(port: int, method: str, params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def test_server_serves_health_card_and_executes_task():
    port = 8943

    @emerge.agent(name="Live Agent", description="echoes", port=port)
    def handle(task: str) -> str:
        return f"echo: {task}"

    httpd = serve_agent(registered_agents()[0], host="127.0.0.1", block=False)
    try:
        assert _get(port, "/health")["status"] == "healthy"
        assert _get(port, "/.well-known/agent.json")["name"] == "Live Agent"

        result = _post_jsonrpc(port, "message/send", {
            "message": {"parts": [{"kind": "text", "text": "hello"}]},
        })["result"]
        assert result["status"]["state"] == "completed"
        answer = result["artifacts"][0]["parts"][0]["text"]
        assert answer == "echo: hello"

        # tasks/get reads the same task back.
        fetched = _post_jsonrpc(port, "tasks/get", {"id": result["id"]})["result"]
        assert fetched["id"] == result["id"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_async_handler_supported():
    port = 8944

    @emerge.agent(name="Async Agent", description="async", port=port)
    async def handle(task: str) -> str:
        return f"async: {task}"

    httpd = serve_agent(registered_agents()[0], host="127.0.0.1", block=False)
    try:
        result = _post_jsonrpc(port, "message/send", {
            "message": {"parts": [{"type": "text", "text": "x"}]},
        })["result"]
        assert result["artifacts"][0]["parts"][0]["text"] == "async: x"
    finally:
        httpd.shutdown()
        httpd.server_close()
