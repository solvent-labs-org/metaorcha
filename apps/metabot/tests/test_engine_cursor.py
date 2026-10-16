from __future__ import annotations

import contextlib
import json

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot.engines.base import EngineJob, run_cli
from metabot.engines.cursor import SWITCHBOARD_PROVIDER, CursorEngine
from metabot.events import Final, Started, TextEvent, ToolCall, ToolResult, Usage
from metabot.steps import StepRecorder

SWITCHBOARD = ["/usr/bin/python3", "-m", "metabot", "mcp", "serve", "--run-dir", "/r"]


def _parse(name: str):
    engine = CursorEngine()
    state: dict = {}
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(engine.parse_line(line, state))
    return events


def _jobs(tmp_path):
    base = {"prompt": "-x", "cwd": tmp_path}
    yield EngineJob(**base)
    yield EngineJob(**base, read_only=True)
    yield EngineJob(**base, model="m", resume="r", plugin_dirs=[tmp_path])
    yield EngineJob(**base, switchboard=SWITCHBOARD, work_dir=tmp_path)


def test_argv_never_force(tmp_path):
    engine = CursorEngine()
    for job in _jobs(tmp_path):
        argv = engine.argv(engine.prepare(job))
        assert not {"--force", "-f", "--yolo", "--auto-review"} & set(argv)
        assert argv[argv.index("--sandbox") + 1] == "enabled"
        assert argv[-2:] == ["--", "-x"]


def test_argv_shape(tmp_path):
    engine = CursorEngine()
    argv = engine.argv(EngineJob(prompt="hi", cwd=tmp_path, model="m", resume="chat-1"))
    assert argv[:4] == ["cursor-agent", "-p", "--output-format", "stream-json"]
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert "--trust" in argv
    assert argv[argv.index("--model") + 1] == "m"
    assert argv[argv.index("--resume") + 1] == "chat-1"
    assert "--approve-mcps" not in argv  # only the run's own switchboard is approved
    assert "--mode" not in argv
    ro = engine.argv(EngineJob(prompt="hi", cwd=tmp_path, read_only=True))
    assert ro[ro.index("--mode") + 1] == "ask"


def test_switchboard_becomes_a_plugin_dir(tmp_path):
    engine = CursorEngine()
    job = EngineJob(prompt="hi", cwd=tmp_path, switchboard=SWITCHBOARD, work_dir=tmp_path)
    job = engine.prepare(job)
    argv = engine.argv(job)
    plugin = tmp_path / "cursor-plugin" / "metabot"
    assert argv[argv.index("--plugin-dir") + 1] == str(plugin)
    assert "--approve-mcps" in argv
    manifest = json.loads((plugin / ".cursor-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "metabot"
    servers = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]
    assert servers == {"metabot": {"command": SWITCHBOARD[0], "args": SWITCHBOARD[1:]}}
    assert SWITCHBOARD_PROVIDER == "plugin-metabot-metabot"


def test_switchboard_without_work_dir_is_refused(tmp_path):
    with pytest.raises(ValueError):
        CursorEngine().prepare(EngineJob(prompt="hi", cwd=tmp_path, switchboard=SWITCHBOARD))


def _project_servers(project, text: str) -> None:
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"mcpServers": {"helper": {"command": "sh", "args": ["-c", "id"]}}}),
        "{ not json",  # what cursor would make of it is unknown: refused
        json.dumps(["not", "an", "object"]),
    ],
)
def test_a_project_with_its_own_mcp_servers_is_refused_with_connections(tmp_path, text):
    # --approve-mcps approves every server cursor loads, and the project's own
    # would run with the person's privileges, outside MetaBot's gate.
    project = tmp_path / "project"
    project.mkdir()
    _project_servers(project, text)
    job = EngineJob(prompt="hi", cwd=project, switchboard=SWITCHBOARD, work_dir=tmp_path)
    with pytest.raises(ValueError, match=r"\.cursor/mcp\.json"):
        CursorEngine().prepare(job)


def test_a_project_file_that_declares_no_server_is_fine(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _project_servers(project, json.dumps({"mcpServers": {}}))
    job = EngineJob(prompt="hi", cwd=project, switchboard=SWITCHBOARD, work_dir=tmp_path)
    assert "--approve-mcps" in CursorEngine().argv(CursorEngine().prepare(job))


def test_without_connections_the_project_servers_are_never_approved(tmp_path):
    _project_servers(tmp_path, json.dumps({"mcpServers": {"helper": {"command": "sh"}}}))
    engine = CursorEngine()
    job = engine.prepare(EngineJob(prompt="hi", cwd=tmp_path))
    assert "--approve-mcps" not in engine.argv(job)


def test_parse_echo_fixture():
    events = _parse("cursor-echo.jsonl")
    assert isinstance(events[0], Started)
    assert events[0].model == "default-model"
    rec = StepRecorder("cursor", skip_tools=CursorEngine.skip_tools)
    for e in events:
        rec.on_event(e)
    [step] = rec.steps()
    assert step["tool"] == "cursor/shellToolCall"
    assert "\n" not in step["call_id"]
    assert step["args"]["command"] == "echo metabot-probe"
    assert "parsingResult" not in step["args"] and "requestId" not in step["args"]
    assert step["success"] is True
    assert step["output"]["success"]["exitCode"] == 0
    assert isinstance(events[-1], Final)
    assert events[-1].text == "metabot-probe"
    assert any(isinstance(e, Usage) for e in events)


def test_thinking_dropped():
    texts = [e.text for e in _parse("cursor-echo.jsonl") if isinstance(e, TextEvent)]
    assert "(model thinking)" not in " ".join(texts)
    assert texts == ["I'll run that command and reply with only its output.", "metabot-probe"]


def test_parse_mcp_fixture_skips_the_switchboard():
    events = _parse("cursor-mcp.jsonl")
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert [c.tool for c in calls] == [
        "getMcpToolsToolCall",
        f"mcp/{SWITCHBOARD_PROVIDER}/metabot_echo",
    ]
    assert calls[1].args == {"text": "spike2"}
    rec = StepRecorder("cursor", skip_tools=CursorEngine.skip_tools)
    for e in events:
        rec.on_event(e)
    assert [s["tool"] for s in rec.steps()] == ["cursor/getMcpToolsToolCall"]


def test_nonzero_exit_inside_success_is_a_failure():
    engine = CursorEngine()
    state: dict = {}
    body = {"args": {"command": "false"}}
    started = {
        "type": "tool_call",
        "subtype": "started",
        "call_id": "c1",
        "tool_call": {"shellToolCall": body},
    }
    result = {"success": {"exitCode": 1, "stdout": "", "stderr": ""}}
    done = {
        "type": "tool_call",
        "subtype": "completed",
        "call_id": "c1",
        "tool_call": {"shellToolCall": {**body, "result": result}},
    }
    engine.parse_line(json.dumps(started), state)
    [res] = engine.parse_line(json.dumps(done), state)
    assert res == ToolResult("c1", result, False)


async def test_runs_through_the_engine_base(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "cursor-echo.jsonl"))
    engine = wrap(CursorEngine)
    async with contextlib.aclosing(run_cli(engine, EngineJob("say hi", tmp_path))) as stream:
        events = [e async for e in stream]
    assert isinstance(events[0], Started)
    assert isinstance(events[-1], Final)


async def test_the_refusal_ends_the_run_with_an_error(tmp_path):
    from metabot.events import ErrorEvent

    project = tmp_path / "project"
    project.mkdir()
    _project_servers(project, json.dumps({"mcpServers": {"helper": {"command": "sh"}}}))
    job = EngineJob(prompt="hi", cwd=project, switchboard=SWITCHBOARD, work_dir=tmp_path)
    async with contextlib.aclosing(run_cli(CursorEngine(), job)) as events:
        got = [event async for event in events]
    assert len(got) == 1 and isinstance(got[0], ErrorEvent)
    assert "--no-connections" in got[0].message
    assert not (tmp_path / "cursor-plugin").exists()  # nothing was written for the run
