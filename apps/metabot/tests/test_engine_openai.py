from __future__ import annotations

import asyncio
import json
import sys

import pytest

from conftest import FAKE_MCP, private_mode
from fake_chat import FakeChat
from fake_chat import answer as _answer
from fake_chat import call as _call
from metabot.approvals import DenyAll, Scripted
from metabot.config import Config
from metabot.connections import ConnectionStore
from metabot.engines import openai_compat
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.receipts import verify_file
from metabot.runner import run_job


def _engine(fake: FakeChat, key_env: str | None = None) -> OpenAIEngine:
    return OpenAIEngine(base_url=fake.url, model="tiny", key_env=key_env, config=Config())


async def _run(fake, tmp_path, approver=None, **kw):
    return await run_job(
        engine=_engine(fake, kw.pop("key_env", None)),
        prompt="go",
        cwd=tmp_path,
        policy=kw.pop("policy", Policy()),
        approver=approver or DenyAll(),
        **kw,
    )


def _env(result):
    return json.loads(result.receipt.read_text())


async def test_tool_loop_reads_file(chat, tmp_path):
    (tmp_path / "README.md").write_text("hello from the readme")
    fake = chat([_call("read_file", {"path": "README.md"}), _answer("done")])
    result = await _run(fake, tmp_path)
    assert result.ok, result.error
    assert result.final_text == "done"
    env = _env(result)
    assert [(s["tool"], s["success"]) for s in env["steps"]] == [("metabot/read_file", True)]
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "hello from the readme",
    }
    assert {"check": "model", "result": "pass", "detail": "openai/tiny"} in env["verdicts"]
    assert verify_file(result.receipt).valid


async def test_path_escape_refused(chat, tmp_path):
    fake = chat([_call("read_file", {"path": "../../etc/passwd"}), _answer("ok")])
    result = await _run(fake, tmp_path)
    [step] = _env(result)["steps"]
    assert step["success"] is False
    assert "outside the job directory" in fake.requests[1]["messages"][-1]["content"]


async def test_shell_destructive_stops(chat, tmp_path):
    (tmp_path / "x").mkdir()
    fake = chat([_call("shell", {"command": "rm -rf x"}, "call_rm"), _answer("could not")])
    result = await _run(fake, tmp_path, approver=DenyAll())
    assert (tmp_path / "x").exists()
    [step] = _env(result)["steps"]
    assert step["tool"] == "metabot/shell" and step["success"] is False
    assert {
        "check": "scope_approval:call_rm",
        "result": "warn",
        "detail": "declined: no human present",
    } in _env(result)["verdicts"]


async def test_shell_read_runs_without_asking(chat, tmp_path):
    fake = chat([_call("shell", {"command": "echo hi"}), _answer("ok")])
    approver = Scripted([])
    result = await _run(fake, tmp_path, approver=approver)
    assert approver.asked == 0
    assert _env(result)["steps"][0]["success"] is True
    assert "hi" in fake.requests[1]["messages"][-1]["content"]


async def test_write_with_approval(chat, tmp_path):
    fake = chat([_call("write_file", {"path": "out/a.txt", "content": "x"}), _answer("ok")])
    result = await _run(fake, tmp_path, approver=Scripted([True]))
    assert (tmp_path / "out" / "a.txt").read_text() == "x"
    assert _env(result)["steps"][0]["success"] is True


async def test_read_only_hides_writers(chat, tmp_path):
    fake = chat([_answer("fine")])
    await _run(fake, tmp_path, read_only=True)
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert offered == {"read_file", "list_dir"}


async def test_max_turns(chat, tmp_path, monkeypatch):
    monkeypatch.setattr(openai_compat, "MAX_TURNS", 3)
    fake = chat(repeat=_call("list_dir", {"path": "."}))
    result = await _run(fake, tmp_path)
    assert result.ok is False
    assert "after 3 turns" in result.error
    assert len(_env(result)["steps"]) == 3


async def test_unreachable_server_is_an_error(tmp_path):
    engine = OpenAIEngine(base_url="http://127.0.0.1:9/v1", model="tiny", config=Config())
    result = await run_job(
        engine=engine, prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    assert result.ok is False
    assert "cannot reach" in result.error
    assert verify_file(result.receipt).valid


async def test_key_comes_from_the_environment_at_call_time(chat, tmp_path, monkeypatch):
    monkeypatch.setenv("TINY_KEY", "k-live")
    fake = chat([_answer("ok")])
    await _run(fake, tmp_path, key_env="TINY_KEY")
    assert fake.headers[0]["Authorization"] == "Bearer k-live"


async def test_bad_arguments_are_a_failed_step(chat, tmp_path):
    bad = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c9", "function": {"name": "read_file", "arguments": "{not json"}}]}}]}  # fmt: skip
    fake = chat([bad, _answer("ok")])
    result = await _run(fake, tmp_path)
    assert result.ok
    assert _env(result)["steps"][0]["success"] is False


async def test_resume_continues_the_session(chat, tmp_path):
    fake = chat([_answer("first"), _answer("second")])
    first = await _run(fake, tmp_path)
    second = await _run(fake, tmp_path, resume=first.session_id)
    assert second.session_id == first.session_id
    contents = [m["content"] for m in fake.requests[1]["messages"]]
    assert "first" in contents
    assert contents.count("go") == 2


async def test_switchboard_tools_go_through_the_switchboard(chat, tmp_path):
    count = tmp_path / "count"
    ConnectionStore().add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count)],
        token="tok-123",
        token_env="FAKE_TOKEN",
    )
    fake = chat([_call("gh__list_issues", {}), _answer("one issue")])
    result = await _run(fake, tmp_path, switchboard=True)
    assert result.ok, result.error
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert "gh__list_issues" in offered
    tools = [s["tool"] for s in _env(result)["steps"]]
    assert len(tools) == 1 and tools[0].endswith("#list_issues")
    assert count.read_text().splitlines() == ["list_issues"]


async def test_server_stderr_goes_to_the_run_log_not_the_terminal(chat, tmp_path, capfd):
    server = tmp_path / "noisy_server.py"
    server.write_text(
        "import runpy, sys\n"
        "print('noisy server says hello', file=sys.stderr, flush=True)\n"
        f"sys.argv = [{str(FAKE_MCP)!r}]\n"
        f"runpy.run_path({str(FAKE_MCP)!r}, run_name='__main__')\n"
    )
    ConnectionStore().add(name="gh", transport="stdio", command=[sys.executable, str(server)])
    fake = chat([_answer("ok")])
    result = await _run(fake, tmp_path, switchboard=True)
    assert result.ok, result.error
    assert "noisy server says hello" not in capfd.readouterr().err
    log = run_dir(result.run_id) / "switchboard.log"
    assert "noisy server says hello" in log.read_text()
    assert private_mode(log) == 0o600


# ── cancelling and stopping (review findings 4 and 5) ────────────────────────

CANCEL_SCRIPT = """
import asyncio, sys, time
from pathlib import Path
from metabot.config import Config
from metabot.engines.base import EngineJob
from metabot.engines.openai_compat import OpenAIEngine

async def main():
    engine = OpenAIEngine(base_url=sys.argv[1], model="tiny", key_env="", config=Config())
    job = EngineJob(prompt="hi", cwd=Path(sys.argv[2]), work_dir=Path(sys.argv[2]))

    async def approve(msg):
        return {"allow": False}

    async def consume():
        async for _ in engine.run(job, approve=approve):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

started = time.monotonic()
asyncio.run(main())
print(f"exited {time.monotonic() - started:.2f}")
"""


def test_a_cancelled_request_never_keeps_the_process_waiting(chat, tmp_path):
    """Ctrl-C mid-request: the process ends now, not when the model answers."""
    import os
    import subprocess

    fake = chat(delay=30)
    script = tmp_path / "cancel.py"
    script.write_text(CANCEL_SCRIPT)
    env = {**os.environ, "METABOT_HOME": str(tmp_path / "home")}
    proc = subprocess.run(
        [sys.executable, "-I", str(script), fake.url, str(tmp_path)],
        capture_output=True, text=True, timeout=20, env=env, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert float(proc.stdout.split()[-1]) < 5


async def _gone(pid: int, within: float = 5.0) -> bool:
    import asyncio
    import os

    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


def _kill(pid: int) -> None:
    import contextlib
    import os
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


async def test_a_cancelled_shell_call_stops_everything_it_started(chat, tmp_path):
    import asyncio

    from metabot.runner import RunCancelled

    fake = chat([_call("shell", {"command": "echo $$ > pgid; sleep 30"}), _answer("ok")])
    task = asyncio.create_task(_run(fake, tmp_path, policy=Policy(allow_write=True)))
    pgid_file = tmp_path / "pgid"
    for _ in range(200):
        if pgid_file.exists() and pgid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pgid = int(pgid_file.read_text())
    try:
        task.cancel()
        with pytest.raises(RunCancelled):
            await task
        assert await _gone(pgid)
    finally:
        _kill(pgid)


async def test_a_timed_out_shell_call_stops_its_children(chat, tmp_path, monkeypatch):
    """A child that holds the output pipe used to keep the call (and the run) waiting."""
    import asyncio

    monkeypatch.setattr(openai_compat, "SHELL_TIMEOUT_S", 1.0)
    command = "bash -c 'while :; do sleep 0.2; done' & echo $! > child; wait"
    fake = chat([_call("shell", {"command": command}), _answer("ok")])
    result = await asyncio.wait_for(_run(fake, tmp_path, policy=Policy(allow_write=True)), 30)
    child = int((tmp_path / "child").read_text())
    try:
        said = json.loads(fake.requests[1]["messages"][-1]["content"])
        assert said["timed_out"] is True and said["exit_code"] == 124  # never an exit of 0
        assert result.ok
        assert await _gone(child)
    finally:
        _kill(child)


async def test_what_a_call_leaves_running_stops_when_the_run_ends(chat, tmp_path):
    command = "sleep 30 > /dev/null 2>&1 & echo $! > child"
    fake = chat([_call("shell", {"command": command}), _answer("ok")])
    result = await _run(fake, tmp_path, policy=Policy(allow_write=True))
    child = int((tmp_path / "child").read_text())
    try:
        assert result.ok
        assert await _gone(child)
    finally:
        _kill(child)


async def test_a_server_that_takes_the_call_but_never_answers_says_so(tmp_path, monkeypatch):
    """A local model still loading is not a server that cannot be reached."""

    async def silent(reader, writer):
        await asyncio.sleep(5)  # takes the connection, sends nothing
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(openai_compat, "HTTP_TIMEOUT_S", 0.3)
    engine = OpenAIEngine(base_url=f"http://127.0.0.1:{port}/v1", model="tiny", config=Config())
    try:
        result = await run_job(
            engine=engine, prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll()
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.ok is False
    assert "cannot reach" not in result.error
    assert f"http://127.0.0.1:{port}/v1 gave no answer in 0.3 seconds" in result.error
    assert "still be loading" in result.error
