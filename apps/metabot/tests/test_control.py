from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import threading
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


async def test_a_request_nested_past_what_can_be_read_is_answered_as_malformed(server):
    srv, seen = server
    reader, writer = await asyncio.open_unix_connection(str(srv.path))
    deep = b'{"op": "step", "step": ' + b"[" * 100_000 + b"]" * 100_000 + b"}\n"
    writer.write(deep)
    await writer.drain()
    assert json.loads(await asyncio.wait_for(reader.readline(), 5)) == {
        "error": "malformed request"
    }  # before: its handler died, and the asker read no reply at all
    writer.close()
    step = {"call_id": "s2", "tool": "x#y", "args": {}, "output": 1, "success": True}
    assert await request(srv.path, {"op": "step", "step": step}) == {"ok": True}
    assert seen["steps"] == [step]


async def test_socket_is_owner_only(server):
    srv, _ = server
    assert private_mode(srv.path) == 0o600


async def test_runs_that_start_at_once_leave_the_file_mode_mask_as_it_was(tmp_path):
    servers = []
    for n in range(3):  # as an office's copies start, side by side
        run_dir = tmp_path / f"run-{n}"
        run_dir.mkdir()
        sock = socket_path(run_dir)
        servers.append(
            ControlServer(
                sock, on_approve=None, on_step=lambda s: None, cleanup_dir=sock.parent != run_dir
            )  # fmt: skip
        )
    mask = os.umask(0o022)
    try:
        await asyncio.gather(*(srv.start() for srv in servers))
        # before: 0o177, so every folder made after it, a desk's worktree too, could not be entered
        assert os.umask(0o022) == 0o022
        assert [private_mode(srv.path) for srv in servers] == [0o600] * 3
    finally:
        os.umask(mask)
        for srv in servers:
            await srv.close()


async def test_another_thread_never_works_under_the_socket_s_mask(tmp_path):
    """A thread runs while servers start, as an office copy's git does: the file mode mask is
    the process's, so a socket's must never be it, or what the thread makes cannot be entered."""
    made, stop, modes = tmp_path / "made", threading.Event(), []
    made.mkdir()

    def making() -> None:  # a folder at a time, as git makes a worktree's
        while not stop.is_set():
            folder = made / "f"
            folder.mkdir()
            modes.append(stat.S_IMODE(folder.stat().st_mode))
            folder.rmdir()

    mask = os.umask(0o022)
    worker = threading.Thread(target=making)
    worker.start()
    try:
        for n in range(200):
            run_dir = tmp_path / f"run-{n}"
            run_dir.mkdir()
            sock = socket_path(run_dir)
            srv = ControlServer(
                sock, on_approve=None, on_step=lambda s: None, cleanup_dir=sock.parent != run_dir
            )  # fmt: skip
            await srv.start()
            await srv.close()
    finally:
        stop.set()
        worker.join()
        os.umask(mask)
    # before: some came out 0o600, folders no one could enter
    assert modes and set(modes) == {0o755}, sorted(set(modes))


async def test_a_start_stopped_midway_leaves_nothing_the_loop_still_watches(tmp_path):
    """A run stopped in the one turn its server takes to start, as the web UI's Stop can stop
    it, closes the server whole: the socket the next run opens is read."""
    steps: list = []
    servers = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        sock = socket_path(tmp_path / name)
        servers.append(
            ControlServer(
                sock,
                on_approve=None,
                on_step=steps.append,
                cleanup_dir=sock.parent != tmp_path / name,
            )
        )
    a, b = servers
    starting = asyncio.create_task(a.start())
    await asyncio.sleep(0)  # it runs to its one wait, as it starts to serve
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, 5)
    await a.close()
    await b.start()
    try:
        # before: b's socket took the number of a's, closed while the loop still watched it,
        # so b was never read and its asker waited out its time
        reply = await request(b.path, {"op": "step", "step": {"n": 1}}, timeout=5)
    finally:
        await b.close()
    assert reply == {"ok": True} and steps == [{"n": 1}]


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


def _waits_for_a_person(sock: Path, tmp_path: Path, steps: list | None = None):
    """A server whose person never answers: ``asked`` is set when they are asked, and
    ``withdrawn`` when the question is taken back from them."""
    asked, withdrawn = asyncio.Event(), asyncio.Event()

    async def on_approve(msg):
        asked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            withdrawn.set()
            raise

    srv = ControlServer(
        sock,
        on_approve=on_approve,
        on_step=(steps if steps is not None else []).append,
        cleanup_dir=sock.parent != tmp_path,
    )
    return srv, asked, withdrawn


@pytest.mark.parametrize("asker", ["cancelled", "timed out", "a thread, timed out"])
async def test_an_asker_that_hangs_up_withdraws_its_question(tmp_path, asker):
    sock = socket_path(Path(tmp_path))
    srv, asked, withdrawn = _waits_for_a_person(sock, tmp_path)
    await srv.start()
    msg = {"op": "approve", "call_id": "c1", "cls": "write"}
    try:
        if asker == "cancelled":  # as the switchboard's call is, when its engine gives up
            client = asyncio.create_task(request(sock, msg))
            await asyncio.wait_for(asked.wait(), 5)
            client.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(client, 5)
        elif asker == "timed out":
            with pytest.raises(TimeoutError):
                await request(sock, msg, timeout=0.5)
        else:  # the gate hook's way
            with pytest.raises(TimeoutError):
                await asyncio.to_thread(request_sync, sock, msg, 0.5)
        assert asked.is_set()
        await asyncio.wait_for(withdrawn.wait(), 5)  # before: asked until the run ended
    finally:
        await srv.close()


async def test_a_step_sent_just_before_the_asker_hangs_up_is_still_taken(server):
    srv, seen = server
    step = {"call_id": "s1", "tool": "x#y", "args": {}, "output": 1, "success": True}
    _, writer = await asyncio.open_unix_connection(str(srv.path))
    writer.write(json.dumps({"op": "step", "step": step}).encode() + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    for _ in range(250):
        if seen["steps"]:
            break
        await asyncio.sleep(0.02)
    assert seen["steps"] == [step]


async def test_a_withdrawn_question_reads_as_a_no_and_steps_are_still_taken(tmp_path):
    sock = socket_path(Path(tmp_path))
    steps: list = []
    srv, asked, withdrawn = _waits_for_a_person(sock, tmp_path, steps)
    await srv.start()
    try:
        client = asyncio.create_task(request(sock, {"op": "approve", "cls": "write"}))
        await asyncio.wait_for(asked.wait(), 5)
        srv.withdraw()  # the run is ending
        await asyncio.wait_for(withdrawn.wait(), 5)
        reply = await asyncio.wait_for(client, 5)
        assert reply == {"allow": False, "reason": "declined: cancelled"}
        step = {"call_id": "s1", "tool": "x#y", "args": {}, "output": 1, "success": False}
        assert await request(sock, {"op": "step", "step": step}) == {"ok": True}
        assert steps == [step]
    finally:
        await srv.close()
