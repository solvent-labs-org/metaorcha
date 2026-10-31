from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import anyio
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


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),  # the runner gave no answer in time
        ConnectionRefusedError(),  # the runner is gone
        ValueError("the runner replied with something other than an object"),
    ],
    ids=["timed-out", "runner-gone", "no-reply"],
)
async def test_a_call_whose_question_got_no_answer_is_recorded_declined(store, count_file, failure):
    steps: list[dict] = []

    async def approve(msg):
        raise failure

    async def record(step):
        steps.append(step)

    async with Switchboard(store, approve=approve, record=record) as board:
        result = await board.call("gh__create_issue", {"title": "x"})  # before: it raised
    assert result.is_error is True
    assert result.content[0].text == "metabot: write call not allowed (declined: cancelled)"
    assert _calls(count_file) == []  # never made
    [step] = steps
    assert step["tool"].endswith("#create_issue") and step["args"] == {"title": "x"}
    assert step["output"] == {"error": "declined: cancelled"} and step["success"] is False


@pytest.mark.parametrize("name", ["gh__create_issues", "skills__load"])
async def test_a_call_to_a_tool_it_does_not_have_is_recorded_failed(store, name):
    h = Harness(store)
    async with h.board as board:
        result = await board.call(name, {"title": "x"})
    assert result.is_error is True
    assert result.content[0].text == f"metabot: no tool named {name}"
    [step] = h.steps  # before: in no record, while the engine said it called it
    assert step["tool"] == f"metabot/{name}" and step["args"] == {"title": "x"}
    assert step["output"] == {"error": f"no tool named {name}"} and step["success"] is False


# Each way a call ends, and the step its record gets: (tool called, args, output, success).
STOPPED_AS_RECORDED = {
    "declined": ("gh__create_issue", {"title": "x"}, {"error": "declined: cancelled"}, False),
    "no-answer": ("gh__create_issue", {"title": "x"}, {"error": "declined: cancelled"}, False),
    "no-such-tool": (
        "gh__create_issues", {"title": "x"}, {"error": "no tool named gh__create_issues"}, False
    ),
    "revoked": ("gh__create_issue", {"title": "x"}, {"error": "connection gh is revoked"}, False),
    "made": ("gh__list_issues", {"repo": "acme/widgets"}, None, True),
}  # fmt: skip


@pytest.mark.parametrize("how", list(STOPPED_AS_RECORDED))
async def test_a_call_s_step_is_kept_when_the_run_stops_as_it_is_recorded(store, count_file, how):
    name, args, output, ok = STOPPED_AS_RECORDED[how]
    steps: list[dict] = []
    recording = asyncio.Event()

    async def approve(msg):
        if how == "no-answer":
            raise TimeoutError()
        return {"allow": False, "reason": "declined: cancelled"}  # a run that is stopping

    async def record(step):
        recording.set()
        await asyncio.sleep(0.2)  # on its way to the runner
        steps.append(step)

    async with Switchboard(store, approve=approve, record=record) as board:
        if how == "revoked":
            ConnectionStore().revoke("gh")
        async with anyio.create_task_group() as calls:
            calls.start_soon(board.call, name, args)
            await recording.wait()
            calls.cancel_scope.cancel()  # the stop signal comes as the step is sent
    [step] = steps  # before: the stop cut it off, and the call was in no record
    assert step["args"] == args and step["success"] is ok
    assert output is None or step["output"] == output
    assert _calls(count_file) == (["list_issues"] if how == "made" else [])


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


@pytest.mark.parametrize("where", ["relative", "moved", "climbs"])
async def test_a_plugin_server_saved_outside_this_home_is_never_started(
    store, tmp_path, monkeypatch, where
):
    # a plugin enabled under a home named by a relative path, as 0.2.0 saved its server, also
    # one that climbs and then spells this home's whole path, or under a home since moved: in
    # the engine's folder, where the switchboard runs, that path can name a file anyone put there
    copy = f"plugins/tools@{'a' * 64}"
    work = tmp_path / "work" / "deep"
    work.mkdir(parents=True)
    word = {
        "relative": f"rh/{copy}/server.py",
        "moved": f"{tmp_path}/old-home/{copy}/server.py",
        "climbs": f"../..{metabot_home()}/{copy}/server.py",
    }[where]
    planted = Path(word) if where == "moved" else work / word
    planted.parent.mkdir(parents=True)
    ran = tmp_path / "ran"
    planted.write_text(f"open({str(ran)!r}, 'w').close()\n", encoding="utf-8")
    store.add(name="tools-db", transport="stdio", command=[sys.executable, word])
    monkeypatch.chdir(work)
    h = Harness(store)
    async with h.board as board:
        names = {t.name for t in board.tools()}
    assert not ran.exists()  # before: it ran, as the person, before any gate
    assert f"metabot plugin enable {'a' * 12} --add-servers" in board.skipped["tools-db"]
    assert "gh__list_issues" in names  # the person's own connections still open


async def test_a_plugin_server_past_a_folder_since_removed_is_refused(store, tmp_path):
    # its home was named past a folder that is gone: no system call can follow that path, so
    # the server could never start, and enabling it again names the copy whole
    copy = metabot_home() / "plugins" / f"tools@{'a' * 64}"
    copy.mkdir(parents=True)
    word = f"{tmp_path}/gone/../{copy.relative_to(tmp_path)}/server.py"
    store.add(name="tools-db", transport="stdio", command=[sys.executable, word])
    h = Harness(store)
    async with h.board as board:
        names = {t.name for t in board.tools()}
    # before: it was started, failed, and nothing said what would mend it
    assert f"metabot plugin enable {'a' * 12} --add-servers" in board.skipped["tools-db"]
    assert "gh__list_issues" in names


@pytest.mark.parametrize(
    "named",
    [
        "whole",
        "by a link",
        "past ..",
        "as an option's value",
        "in a shell line",
        "quoted in a shell line",
    ],
)
async def test_a_plugin_server_naming_its_copy_in_this_home_starts(
    store, tmp_path, count_file, named
):
    copy = metabot_home() / "plugins" / f"tools@{'a' * 64}"
    copy.mkdir(parents=True)
    (tmp_path / "link").symlink_to(metabot_home())
    (tmp_path / "next-door").mkdir()
    spelled = {
        "by a link": tmp_path / "link" / copy.relative_to(metabot_home()),
        "past ..": tmp_path / "next-door" / ".." / copy.relative_to(tmp_path),
    }.get(named, copy)
    # as enable names it: the copy whole, in an argument too, the home named as it was then
    command = [sys.executable, str(FAKE_MCP), "--count-file", f"{spelled}/calls"]
    if named == "as an option's value":
        command = [*command[:2], "=".join(command[2:])]
    if named.endswith("in a shell line"):  # a plugin's server may be a line for sh
        mark = '"' if named.startswith("quoted") else ""
        line = f"exec {sys.executable} {FAKE_MCP} --count-file {mark}{spelled}/calls{mark}"
        command = ["sh", "-c", line]
    store.add(name="tools-db", transport="stdio", command=command)
    h = Harness(store)
    async with h.board as board:
        names = {t.name for t in board.tools()}
    # before: a link or a .. in the home's name refused it, and so did a shell line
    assert board.skipped == {}
    assert {"tools-db__list_issues", "gh__list_issues"} <= names


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


async def test_a_read_only_job_is_offered_a_connection_s_reads_alone(store, tmp_path):
    from metabot.config import Config
    from metabot.engines.base import EngineJob
    from metabot.engines.openai_compat import OpenAIEngine

    h = Harness(store)
    engine = OpenAIEngine(base_url="http://127.0.0.1:9/v1", model="tiny", config=Config())
    async with h.board as board:
        every = {t.name: t.description for t in board.tools()}
        reads = {t.name for t in board.tools(read_only=True)}
        offered = engine._tools(EngineJob("go", tmp_path, read_only=True), board)
    assert reads == {name for name, said in every.items() if said.startswith("[read]")}
    assert "gh__list_issues" in reads and not {"gh__create_issue", "gh__delete_repo"} & reads
    names = {tool["function"]["name"] for tool in offered}
    assert "gh__list_issues" in names and "read_file" in names
    assert not {"gh__create_issue", "gh__delete_repo", "write_file", "shell"} & names


async def test_a_read_only_openai_job_cannot_call_a_tool_it_was_not_offered(
    store, count_file, chat, tmp_path
):
    from fake_chat import answer, call
    from metabot.commands import ask_once
    from metabot.config import Config
    from metabot.engines.openai_compat import OpenAIEngine
    from metabot.paths import run_path

    script = [call("gh__create_issue", {"title": "x"}, "c1"), answer("done")]
    fake = chat(script)
    result = await ask_once(
        engine_name="openai", engine=OpenAIEngine(base_url=fake.url, model="tiny", config=Config()),
        prompt="read the issues", cwd=tmp_path, read_only=True, allow_write=True,
        allows=["gh#create_issue"], approver=DenyAll(), plugins=False,
    )  # fmt: skip
    assert "create_issue" not in (count_file.read_text() if count_file.exists() else "")
    said = fake.requests[1]["messages"][-1]
    assert said["role"] == "tool" and said["content"] == "no tool named gh__create_issue"
    policy = json.loads((run_path(result.run_id) / "policy.json").read_text(encoding="utf-8"))
    assert policy["allow_write"] is False and not policy["allows"]  # every write is asked
    steps = json.loads(result.receipt.read_text(encoding="utf-8"))["steps"]
    assert [(s["tool"], s["success"]) for s in steps] == [("metabot/gh__create_issue", False)]
    assert result.steps == 1 and result.failed == 1  # refused, and recorded as such
