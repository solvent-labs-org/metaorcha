from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from conftest import private_mode
from metabot import control
from metabot.control import ControlServer, request, request_sync, socket_path


@pytest.fixture
async def server(tmp_path):
    sock = socket_path(Path(tmp_path))
    seen: dict = {"steps": [], "asks": []}

    async def on_approve(msg):
        seen["asks"].append(msg)
        return {"allow": msg.get("cls") == "write", "reason": "test"}

    srv = ControlServer(
        sock,
        on_approve=on_approve,
        on_step=seen["steps"].append,
        cleanup_dir=sock.parent != tmp_path,
    )
    await srv.start()
    yield srv, seen
    await srv.close()


async def test_approve_round_trip(server):
    srv, seen = server
    reply = await request(srv.path, {"op": "approve", "call_id": "c1", "cls": "write"})
    assert reply == {"allow": True, "reason": "test"}
    assert seen["asks"][0]["call_id"] == "c1"


async def test_step_round_trip(server):
    srv, seen = server
    step = {"call_id": "s1", "tool": "x#y", "args": {}, "output": 1, "success": True}
    assert await request(srv.path, {"op": "step", "step": step}) == {"ok": True}
    assert seen["steps"] == [step]


async def test_unknown_op_and_malformed(server):
    srv, _ = server
    assert await request(srv.path, {"op": "nope"}) == {"error": "unknown op"}
    reader, writer = await asyncio.open_unix_connection(str(srv.path))
    writer.write(b"{not json\n")
    await writer.drain()
    assert b"malformed" in await reader.readline()
    writer.close()


async def test_socket_is_owner_only(server):
    srv, _ = server
    assert private_mode(srv.path) == 0o600


async def test_sync_client_from_a_thread(server):
    srv, _ = server
    reply = await asyncio.to_thread(
        request_sync, srv.path, {"op": "approve", "call_id": "c2", "cls": "destructive"}
    )
    assert reply == {"allow": False, "reason": "test"}


async def test_handler_bug_is_an_error_reply(tmp_path):
    async def broken(msg):
        raise RuntimeError("boom")

    sock = socket_path(tmp_path)
    srv = ControlServer(sock, on_approve=broken, on_step=lambda s: None)
    await srv.start()
    try:
        reply = await request(sock, {"op": "approve"})
    finally:
        await srv.close()
    assert "boom" in reply["error"]
    assert not sock.exists()


def test_long_run_dir_gets_a_short_private_socket(tmp_path):
    deep = tmp_path / ("d" * 120)
    path = socket_path(deep)
    assert len(os.fsencode(str(path))) <= control.SUN_PATH_LIMIT
    assert private_mode(path.parent) == 0o700
    path.parent.rmdir()


def test_a_long_temp_folder_still_gives_a_socket_that_fits(tmp_path, monkeypatch):
    long_tmp = tmp_path / ("t" * 100)
    long_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(long_tmp))
    path = socket_path(tmp_path / ("d" * 120))
    try:
        assert len(os.fsencode(str(path))) <= control.SUN_PATH_LIMIT
        assert private_mode(path.parent) == 0o700
    finally:
        path.parent.rmdir()


async def test_close_does_not_wait_on_a_pending_answer(tmp_path):
    sock = socket_path(Path(tmp_path))
    asked = asyncio.Event()

    async def on_approve(msg):
        asked.set()
        await asyncio.Event().wait()  # a person who never answers

    srv = ControlServer(
        sock, on_approve=on_approve, on_step=lambda s: None, cleanup_dir=sock.parent != tmp_path
    )
    await srv.start()
    client = asyncio.create_task(request(sock, {"op": "approve", "cls": "destructive"}))
    await asyncio.wait_for(asked.wait(), 5)
    await asyncio.wait_for(srv.close(), 2)  # before: waited on the person forever
    with pytest.raises(ValueError):  # no reply, which every asker reads as a no
        await asyncio.wait_for(client, 2)
