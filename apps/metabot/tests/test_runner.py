from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot.approvals import DenyAll, Scripted
from metabot.control import ControlServer
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.events import Final, Started
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.receipts import journal_matches, verify_file
from metabot.runner import RunCancelled, run_job


def _claude():
    return wrap(ClaudeEngine)


def _envelope(result):
    return json.loads(result.receipt.read_text())


async def _run(tmp_path, approver=None, **kw):
    return await run_job(
        engine=_claude(),
        prompt="say metabot-probe",
        cwd=tmp_path,
        policy=Policy(),
        approver=approver or DenyAll(),
        **kw,
    )


async def test_run_seals_valid_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path)
    assert result.ok is True, result.error
    assert result.steps == 1
    assert result.final_text == "metabot-probe"
    assert result.session_id == "00000000-0000-4000-8000-000000000001"
    assert verify_file(result.receipt).valid is True
    assert journal_matches(result.run_id) is True
    env = _envelope(result)
    assert {"check": "model", "result": "pass", "detail": "claude/claude-haiku-5-5"} in env[
        "verdicts"
    ]
    assert env["steps"][0]["tool"] == "claude-code/Bash"


async def test_the_model_recorded_is_the_one_the_engine_ran(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path, model="haiku")
    env = _envelope(result)
    assert {"check": "model", "result": "pass", "detail": "claude/claude-haiku-5-5"} in env[
        "verdicts"
    ]
    journal = json.loads((run_dir(result.run_id) / "journal.json").read_text())
    assert journal["meta"]["model"] == "claude-haiku-5-5"
    assert journal["meta"]["model_requested"] == "haiku"


async def test_notices_are_kept_and_do_not_fail_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-notices.jsonl"))
    result = await run_job(
        engine=wrap(CodexEngine),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
    )
    assert result.ok is True, result.error
    meta = json.loads((run_dir(result.run_id) / "journal.json").read_text())["meta"]
    assert meta["notices"] == [
        "codex: Reconnecting... 1/5",
        "codex: Skill descriptions were shortened to fit the skills context budget.",
    ]


async def test_engine_crash_still_seals(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_EXIT_AFTER", "2")  # init + the tool call, then die
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.setenv("FAKE_STDERR", "session expired, run claude login")
    result = await _run(tmp_path)
    assert result.ok is False
    assert "exited 3" in result.error and "claude login" in result.error
    assert verify_file(result.receipt).valid is True
    [step] = _envelope(result)["steps"]
    assert step["success"] is False  # the call never got its result


class _Answers:
    """An in-process engine: it runs no subprocess, so no socket is ever served."""

    name = "answers"
    tool_prefix = "fake"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        yield Final("done", "s-1")


@pytest.fixture
def short_tmp(tmp_path, monkeypatch):
    """A private temp folder, short enough for a socket, as the system's temp folder."""
    path = Path(tempfile.mkdtemp(prefix="t-", dir="/tmp"))
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    # a home this long puts the run's socket in a temp folder
    monkeypatch.setenv("METABOT_HOME", str(tmp_path / ("h" * 60)))
    yield path
    shutil.rmtree(path)


async def test_an_in_process_run_leaves_no_socket_folder(tmp_path, short_tmp):
    result = await run_job(
        engine=_Answers(), prompt="hi", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    assert result.ok
    assert list(short_tmp.iterdir()) == []


async def test_a_socket_that_cannot_be_served_leaves_no_folder(tmp_path, monkeypatch, short_tmp):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))

    async def refuse(self):
        raise OSError("AF_UNIX path too long")

    monkeypatch.setattr(ControlServer, "start", refuse)
    with pytest.raises(OSError, match="too long"):
        await _run(tmp_path)
    assert list(short_tmp.iterdir()) == []


async def test_external_step_lands_in_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    step = {
        "call_id": "sw-1",
        "tool": "did:orcha:agent:conn-0123456789ab#list_issues",
        "args": {"repo": "acme/widgets"},
        "output": [],
        "success": True,
        "latency_ms": 7,
    }
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps({"op": "step", "step": step}))
    result = await _run(tmp_path)
    assert result.ok is True, result.error
    tools = [s["tool"] for s in _envelope(result)["steps"]]
    assert tools == ["claude-code/Bash", "did:orcha:agent:conn-0123456789ab#list_issues"]
    assert journal_matches(result.run_id) is True


async def test_a_home_named_by_a_relative_path_is_reached_from_the_engines_folder(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METABOT_HOME", "rel-home")
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    step = {
        "call_id": "sw-1",
        "tool": "did:orcha:agent:conn-0123456789ab#list_issues",
        "args": {},
        "output": [],
        "success": True,
        "latency_ms": 7,
    }
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps({"op": "step", "step": step}))
    work = tmp_path / "work"  # the engine runs here, as a desk or `ask --cwd` does
    work.mkdir()
    result = await _run(work)
    # before: the hook was told rel-home/runs/<id>/control.sock, which it looked for in the
    # engine's folder, so the gate could not be reached and the step was lost
    assert result.ok is True, result.error
    tools = [s["tool"] for s in _envelope(result)["steps"]]
    assert tools == ["claude-code/Bash", "did:orcha:agent:conn-0123456789ab#list_issues"]
    assert not (work / "rel-home").exists()


async def test_no_tty_denial_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    ask = {"op": "approve", "call_id": "toolu_x", "tool": "claude-code/Bash", "cls": "destructive"}
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    result = await _run(tmp_path, approver=DenyAll())
    assert {
        "check": "scope_approval:toolu_x",
        "result": "warn",
        "detail": "declined: no human present",
    } in _envelope(result)["verdicts"]


async def test_human_approval_names_the_signer(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    ask = {"op": "approve", "call_id": "toolu_y", "tool": "claude-code/Write", "cls": "write"}
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    approver = Scripted([True])
    result = await _run(tmp_path, approver=approver)
    env = _envelope(result)
    [verdict] = [v for v in env["verdicts"] if v["check"] == "scope_approval:toolu_y"]
    assert verdict == {
        "check": "scope_approval:toolu_y",
        "result": "pass",
        "detail": env["signer"]["did"],
    }
    assert approver.asked == 1


async def test_write_allowed_by_policy_asks_nobody(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    ask = {"op": "approve", "call_id": "toolu_z", "tool": "claude-code/Edit", "cls": "write"}
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    approver = Scripted([])
    result = await run_job(
        engine=_claude(),
        prompt="p",
        cwd=tmp_path,
        policy=Policy(allow_write=True),
        approver=approver,
    )
    assert approver.asked == 0
    env = _envelope(result)
    assert not any(v["check"].startswith("scope_approval") for v in env["verdicts"])  # nobody did
    allowed = {"check": "scope_allow:toolu_z", "result": "pass", "detail": "allow_write"}
    assert allowed in env["verdicts"]  # the receipt signs which allow let the write run
    assert verify_file(result.receipt).valid is True and journal_matches(result.run_id) is True


async def test_missing_final_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_EXIT_AFTER", "4")  # stops before the result line, exits 0
    result = await _run(tmp_path)
    assert result.ok is False
    assert "without a final answer" in result.error
    assert verify_file(result.receipt).valid is True


async def test_cancel_still_seals(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_EXIT_AFTER", "2")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    seen = asyncio.Event()

    def on_event(event):
        if isinstance(event, Started):
            seen.set()

    task = asyncio.ensure_future(_run(tmp_path, on_event=on_event))
    await asyncio.wait_for(seen.wait(), 10)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(RunCancelled) as info:
        await asyncio.wait_for(task, 15)
    result = info.value.result
    assert result.ok is False and result.error == "cancelled"
    assert verify_file(result.receipt).valid is True
    assert result.steps == 1


class NeverAnswers:
    """A person at the terminal who never answers."""

    kind = "human"

    def __init__(self) -> None:
        self.asked = asyncio.Event()

    async def ask(self, req):
        self.asked.set()
        await asyncio.Event().wait()


async def test_cancel_while_a_person_is_asked_still_seals(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    ask = {"op": "approve", "call_id": "toolu_rm", "tool": "claude-code/Bash", "cls": "destructive"}
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    approver = NeverAnswers()
    task = asyncio.ensure_future(_run(tmp_path, approver=approver))
    await asyncio.wait_for(approver.asked.wait(), 10)
    task.cancel()
    with pytest.raises(RunCancelled) as info:  # before: hung until the person answered
        await asyncio.wait_for(task, 5)
    env = _envelope(info.value.result)
    assert {
        "check": "scope_approval:toolu_rm",
        "result": "warn",
        "detail": "declined: cancelled",
    } in env["verdicts"]
    assert verify_file(info.value.result.receipt).valid is True


async def test_hook_command_is_wired_for_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    await _run(tmp_path)
    argv = json.loads(out.read_text())["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "-I -m metabot hook pre-tool-use --control" in command
    assert "--switchboard" not in command
    assert "--permission-prompts" in argv


async def test_switchboard_wired_and_tokens_redacted(tmp_path, monkeypatch):
    import sys

    from conftest import FAKE_MCP
    from metabot.connections import ConnectionStore
    from metabot.paths import run_dir

    ConnectionStore().add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP)],
        token="metabot-probe",  # the fixture's own output: it must not survive into the record
        token_env="FAKE_TOKEN",
    )
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    result = await _run(tmp_path, switchboard=True)
    argv = json.loads(out.read_text())["argv"]
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    args = config["mcpServers"]["metabot"]["args"]
    assert args[:5] == ["-I", "-m", "metabot", "mcp", "serve"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "--switchboard" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--control" in args and "--run-dir" in args
    journal = (run_dir(result.run_id) / "journal.json").read_text()
    assert "metabot-probe" not in journal
    assert "[REDACTED:FAKE_TOKEN]" in journal
    assert "metabot-probe" not in result.receipt.read_text()
    assert verify_file(result.receipt).valid is True
    assert journal_matches(result.run_id) is True


async def test_without_a_switchboard_a_lookalike_tool_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-mcp-and-error.jsonl"))
    result = await _run(tmp_path)
    tools = [s["tool"] for s in _envelope(result)["steps"]]
    assert "claude-code/mcp__metabot__gh__list_issues" in tools


async def test_tokens_are_redacted_without_a_switchboard(tmp_path, monkeypatch):
    """An engine can read a token from disk in any run; it never reaches the record."""
    from metabot.connections import ConnectionStore

    ConnectionStore().add(
        name="gh",
        transport="stdio",
        command=["true"],
        token="metabot-probe",  # the fixture's own output
        token_env="FAKE_TOKEN",
    )
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path)
    journal = (run_dir(result.run_id) / "journal.json").read_text()
    assert "metabot-probe" not in journal
    assert "[REDACTED:FAKE_TOKEN]" in journal
    assert "metabot-probe" not in result.receipt.read_text()
    assert verify_file(result.receipt).valid is True
