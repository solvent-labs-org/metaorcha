from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import pytest

from conftest import FAKE_MCP
from metabot.connections import ConnectionStore
from metabot.upstream import Upstream


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_MCP), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(10)


async def test_stdio_list_and_call(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_SECRET", "must-not-leak")
    count = tmp_path / "count"
    store = ConnectionStore()
    conn = store.add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count)],
        token="tok-123",
        token_env="FAKE_TOKEN",
    )
    async with Upstream(conn, store.secret(conn)) as up:
        names = [t.name for t in await up.list_tools()]
        assert names == ["list_issues", "create_issue", "delete_repo", "frobnicate", "echo_env"]
        result = await up.call_tool("echo_env", {})
    seen = json.loads(result.content[0].text)
    assert seen["FAKE_TOKEN"] == "tok-123"
    assert seen["HOME_SECRET"] is None
    assert count.read_text().splitlines() == ["echo_env"]


async def test_http_bearer_header(http_server):
    store = ConnectionStore()
    conn = store.add(name="h", transport="http", url=http_server, token="tok-http")
    async with Upstream(conn, store.secret(conn)) as up:
        result = await up.call_tool("echo_env", {})
    seen = json.loads(result.content[0].text)
    assert seen["authorization"] == "Bearer tok-http"
    assert seen["FAKE_TOKEN"] is None


async def test_http_custom_header_carries_the_raw_token(http_server):
    store = ConnectionStore()
    conn = store.add(name="k", transport="http", url=http_server, token="k-1", header="X-API-Key")
    async with Upstream(conn, store.secret(conn)) as up:
        seen = json.loads((await up.call_tool("echo_env", {})).content[0].text)
    assert seen["x-api-key"] == "k-1"
    assert seen["authorization"] is None


async def test_unknown_tool_is_an_error_result(tmp_path):
    store = ConnectionStore()
    conn = store.add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    async with Upstream(conn, None) as up:
        result = await up.call_tool("no_such_tool", {})
    assert result.is_error is True


async def test_a_server_that_will_not_start_raises(tmp_path):
    store = ConnectionStore()
    conn = store.add(name="gone", transport="stdio", command=[str(tmp_path / "missing-server")])
    with pytest.raises(Exception):  # noqa: B017 - any failure to connect is fine; it must not hang
        async with Upstream(conn, None, timeout=10):
            pass
