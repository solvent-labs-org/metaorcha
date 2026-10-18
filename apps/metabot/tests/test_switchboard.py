from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys

import pytest

from conftest import FAKE_MCP
from metabot.approvals import DenyAll, Scripted
from metabot.connections import ConnectionStore
from metabot.control import ControlServer, socket_path
from metabot.decisions import Decider
from metabot.gate import Policy
from metabot.paths import metabot_home
from metabot.switchboard import Switchboard

SIGNER = "did:orcha:agent:key-0000000000000000"


@pytest.fixture
def count_file(tmp_path):
    return tmp_path / "count"


@pytest.fixture
def store(count_file):
    store = ConnectionStore()
    store.add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count_file)],
        token="tok-123",
        token_env="FAKE_TOKEN",
    )
    return store


def _calls(count_file) -> list[str]:
    return count_file.read_text().splitlines() if count_file.exists() else []


class Harness:
    def __init__(self, store, policy=None, approver=None):
        self.decider = Decider(policy or Policy(), approver or Scripted([]), SIGNER)
        self.steps: list[dict] = []
        self.board = Switchboard(store, approve=self.decider, record=self._record)

    async def _record(self, step):
        self.steps.append(step)


async def test_tools_are_named_and_labelled(store):
    h = Harness(store)
    async with h.board as board:
        listed = {t.name: t.description for t in board.tools()}
    assert set(listed) == {
        "gh__list_issues",
        "gh__create_issue",
        "gh__delete_repo",
        "gh__frobnicate",
        "gh__echo_env",
    }
    assert listed["gh__list_issues"].startswith("[read]")
    assert listed["gh__create_issue"].startswith("[write]")
    assert listed["gh__delete_repo"].startswith("[destructive]")
    assert listed["gh__frobnicate"].startswith("[destructive]")  # unrecognised


async def test_read_passes(store, count_file):
    h = Harness(store, approver=Scripted([]))
    async with h.board as board:
        result = await board.call("gh__list_issues", {"repo": "acme/widgets"})
    assert result.is_error is not True
    assert h.decider.approver.asked == 0
    [step] = h.steps
    conn = store.get("gh")
    assert step["tool"] == f"{conn.id}#list_issues"
    assert step["success"] is True
    assert _calls(count_file) == ["list_issues"]


async def test_write_asks_then_runs(store, count_file):
    h = Harness(store, approver=Scripted([True]))
    async with h.board as board:
        result = await board.call("gh__create_issue", {"title": "x"})
    assert result.is_error is not True
    assert h.decider.approver.asked == 1
    assert _calls(count_file) == ["create_issue"]
    assert h.decider.verdicts[0]["result"] == "pass"


async def test_write_allowed_by_policy(store, count_file):
    h = Harness(store, policy=Policy(allow_write=True), approver=Scripted([]))
    async with h.board as board:
        await board.call("gh__create_issue", {"title": "x"})
    assert h.decider.approver.asked == 0
    assert _calls(count_file) == ["create_issue"]
    [step] = h.steps
    assert h.decider.verdicts == [
        {"check": f"scope_allow:{step['call_id']}", "result": "pass", "detail": "allow_write"}
    ]


async def test_destructive_denied_without_yes(store, count_file):
    h = Harness(store, approver=Scripted([False]))
    async with h.board as board:
        result = await board.call("gh__delete_repo", {"repo": "acme/widgets"})
    assert result.is_error is True
    assert _calls(count_file) == []
    [step] = h.steps
    assert step["success"] is False
    assert h.decider.verdicts == [
        {"check": f"scope_approval:{step['call_id']}", "result": "warn", "detail": "declined"}
    ]


async def test_unrecognised_is_destructive_and_no_human_denies(store, count_file):
    h = Harness(store, approver=DenyAll())
    async with h.board as board:
        result = await board.call("gh__frobnicate", {})
    assert result.is_error is True
    assert "no human present" in result.content[0].text
    assert _calls(count_file) == []


async def test_revoked_fails_closed_before_request(store, count_file):
    h = Harness(store, approver=Scripted([]))
    async with h.board as board:
        ConnectionStore().revoke("gh")  # another terminal, mid-run
        result = await board.call("gh__list_issues", {})
    assert result.is_error is True
    assert "revoked" in result.content[0].text
    assert _calls(count_file) == []
    [step] = h.steps
    assert step["success"] is False


async def test_token_echo_redacted(store):
    h = Harness(store, approver=Scripted([True]))
    async with h.board as board:
        result = await board.call("gh__echo_env", {})
    text = json.dumps(result.model_dump(mode="json"))
    assert "tok-123" not in text
    assert "[REDACTED:FAKE_TOKEN]" in text
    assert "tok-123" not in json.dumps(h.steps)


async def test_override_tightens_a_read(store, count_file):
    ConnectionStore().set_override("gh", "list_issues", "write")
    h = Harness(store, approver=Scripted([False]))
    async with h.board as board:
        result = await board.call("gh__list_issues", {})
    assert result.is_error is True
    assert h.decider.approver.asked == 1
    assert _calls(count_file) == []


async def test_a_dead_connection_is_skipped(store, tmp_path):
    store.add(name="gone", transport="stdio", command=[str(tmp_path / "no-such-server")])
    h = Harness(store)
    async with h.board as board:
        names = {t.name for t in board.tools()}
    assert "gone" in board.skipped
    assert "gh__list_issues" in names


async def test_serve_stdio_end_to_end(store, tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    sock = socket_path(run_dir)
    steps: list[dict] = []
    decider = Decider(Policy(), DenyAll(), SIGNER)
    server = ControlServer(
        sock, on_approve=decider, on_step=steps.append, cleanup_dir=sock.parent != run_dir
    )
    await server.start()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "metabot", "mcp", "serve", "--home", str(metabot_home()),
                  "--run-dir", str(run_dir), "--control", str(sock)],
            env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )  # fmt: skip
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 5
            ok = await session.call_tool("gh__list_issues", {})
            denied = await session.call_tool("gh__delete_repo", {"repo": "r"})
        assert ok.is_error is not True
        assert denied.is_error is True
        assert [s["success"] for s in steps] == [True, False]
        assert decider.verdicts[0]["detail"] == "declined: no human present"
    finally:
        await server.close()
    await asyncio.sleep(0)


# ── what servers say ─────────────────────────────────────────────────────────

TOKEN = "tok-123-very-secret"


def _talkative(store: ConnectionStore) -> None:
    store.add(
        name="loud",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--say-token"],
        token=TOKEN,
        token_env="FAKE_TOKEN",
    )


async def test_a_server_that_prints_its_token_never_puts_it_in_the_log(tmp_path):
    store = ConnectionStore()
    _talkative(store)
    log_path = tmp_path / "switchboard.log"
    with open(log_path, "a", encoding="utf-8") as errlog:
        h = Harness(store)
        h.board.errlog = errlog
        async with h.board as board:
            assert "loud__list_issues" in {t.name for t in board.tools()}
    text = log_path.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert TOKEN[: len(TOKEN) // 2] not in text  # no half of it either
    assert "starting with FAKE_TOKEN=[REDACTED:FAKE_TOKEN]\n" in text
    assert "split [REDACTED:FAKE_TOKEN] end\n" in text
    assert "x" * 200_000 + "[REDACTED:FAKE_TOKEN]" + "y" * 10 + "\n" in text


async def test_a_skipped_connection_never_logs_its_token(tmp_path, monkeypatch):
    store = ConnectionStore()
    _talkative(store)
    log_path = tmp_path / "switchboard.log"

    async def refuse(conn):
        raise RuntimeError(f"the server said: bad token {TOKEN}")

    with open(log_path, "a", encoding="utf-8") as errlog:
        h = Harness(store)
        h.board.errlog = errlog
        monkeypatch.setattr(h.board, "_open", refuse)
        async with h.board as board:
            assert board.tools() == []
    text = log_path.read_text(encoding="utf-8")
    assert TOKEN not in text and TOKEN not in board.skipped["loud"]
    assert "connection loud skipped: the server said: bad token [REDACTED:FAKE_TOKEN]" in text


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # a zombie still answers kill(0); it has exited all the same
    with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
        return fh.read().rsplit(")", 1)[1].split()[0] == "Z"


INTERRUPT_SCRIPT = """
import asyncio, sys
from metabot.connections import ConnectionStore
from metabot.switchboard import Switchboard

fake, pid_file = sys.argv[1], sys.argv[2]
store = ConnectionStore()
store.add(name="first", transport="stdio", command=[sys.executable, fake, "--pid-file", pid_file],
          token="tok-1", token_env="FAKE_TOKEN")
store.add(name="second", transport="stdio", command=[sys.executable, fake],
          token="tok-2", token_env="FAKE_TOKEN")


async def approve(msg):
    return {"allow": False}


async def record(step):
    pass


async def main():
    board = Switchboard(store, approve=approve, record=record)
    real_open = board._open

    async def open_then_cancel(conn):
        if conn.name == "second":
            raise asyncio.CancelledError  # stands in for Ctrl-C while servers start
        return await real_open(conn)

    board._open = open_then_cancel
    try:
        async with board:
            pass
    except asyncio.CancelledError:
        print("cancelled", flush=True)


asyncio.run(main())
"""


def test_a_board_interrupted_while_opening_stops_what_it_started(tmp_path):
    pid_file = tmp_path / "pid"
    script = tmp_path / "interrupt.py"
    script.write_text(INTERRUPT_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(FAKE_MCP), str(pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        out, err = "", "hung"
    finally:
        pid = int(pid_file.read_text()) if pid_file.exists() else None
        server_gone = pid is None or _gone(pid)
        if not server_gone:
            os.kill(pid, signal.SIGKILL)  # the server outlived the interrupted open
    assert (proc.returncode, out, err) == (0, "cancelled\n", "")
    assert pid is not None and server_gone


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT, "end of input"])
def test_a_stopped_switchboard_stops_its_servers(tmp_path, home, stop):
    import select
    import time

    pid_file = tmp_path / "pid"
    ConnectionStore().add(
        name="slow",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--linger", "--pid-file", str(pid_file)],
        token="tok-1",
        token_env="FAKE_TOKEN",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "metabot", "mcp", "serve", "--home", str(home),
         "--run-dir", str(run_dir), "--control", str(run_dir / "none.sock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    pid = None
    try:
        hello = json.loads(json.dumps(INIT))
        hello["params"]["clientInfo"]["name"] = "n" * 300_000  # past a default 64 KiB line
        proc.stdin.write(json.dumps(hello) + "\n")
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        assert ready and '"result"' in proc.stdout.readline()  # the board is open
        pid = int(pid_file.read_text())
        if stop == "end of input":
            proc.stdin.close()
        else:
            proc.send_signal(stop)
        proc.wait(timeout=15)
        deadline = time.monotonic() + 5
        while not _gone(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _gone(pid), "the server outlived the switchboard"
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None and not _gone(pid):
            os.kill(pid, signal.SIGKILL)
        proc.stdin.close()
        proc.stdout.close()
