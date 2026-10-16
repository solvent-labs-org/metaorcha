from __future__ import annotations

import contextlib
import json
import tomllib

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot.config import Config
from metabot.engines.base import EngineJob, run_cli
from metabot.engines.codex import CodexEngine
from metabot.events import (
    ErrorEvent,
    Final,
    Notice,
    Started,
    TextEvent,
    ToolCall,
    ToolResult,
    Usage,
)
from metabot.steps import StepRecorder

SWITCHBOARD = ["/opt/py 3/bin/python", "-m", "metabot", "mcp", "serve", "--run-dir", '/r/"q"']


def _parse(name: str):
    engine = CodexEngine()
    state: dict = {}
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(engine.parse_line(line, state))
    return events


def _overrides(argv: list[str]) -> dict[str, str]:
    out = {}
    for i, a in enumerate(argv):
        if a == "-c":
            key, _, value = argv[i + 1].partition("=")
            out[key] = value
    return out


def test_argv_minimal(tmp_path):
    argv = CodexEngine(Config()).argv(EngineJob(prompt="-x hi", cwd=tmp_path))
    assert argv[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert "--ignore-user-config" in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[-2:] == ["--", "-x hi"]
    o = _overrides(argv)
    assert o["sandbox_mode"] == '"workspace-write"'
    assert o["approval_policy"] == '"never"'
    assert not any("dangerously" in a for a in argv)


def test_argv_without_a_switchboard_sets_no_server(tmp_path):
    argv = CodexEngine(Config()).argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert not [a for a in argv if a.startswith("mcp_servers.")]


def test_argv_read_only_and_model(tmp_path):
    job = EngineJob(prompt="hi", cwd=tmp_path, read_only=True, model="gpt-5.5-codex")
    argv = CodexEngine(Config(codex_ignore_user_config=False)).argv(job)
    assert _overrides(argv)["sandbox_mode"] == '"read-only"'
    assert argv[argv.index("-m") + 1] == "gpt-5.5-codex"
    assert "--ignore-user-config" not in argv


def test_argv_switchboard_is_valid_toml(tmp_path):
    job = EngineJob(prompt="hi", cwd=tmp_path, switchboard=SWITCHBOARD)
    o = _overrides(CodexEngine(Config()).argv(job))
    command = o["mcp_servers.metabot.command"]
    args = o["mcp_servers.metabot.args"]
    # codex must not ask (and refuse) before the switchboard's own gate decides.
    assert o["mcp_servers.metabot.default_tools_approval_mode"] == '"approve"'
    assert tomllib.loads(f"v = {command}")["v"] == SWITCHBOARD[0]
    assert tomllib.loads(f"v = {args}")["v"] == SWITCHBOARD[1:]


def test_argv_resume(tmp_path):
    job = EngineJob(prompt="and then?", cwd=tmp_path, resume="00000000-0000-4000-8000-00000000000a")
    argv = CodexEngine(Config()).argv(job)
    assert argv[:3] == ["codex", "exec", "resume"]
    assert argv[-3:] == ["--", "00000000-0000-4000-8000-00000000000a", "and then?"]
    assert "-C" not in argv and "-s" not in argv  # resume takes neither
    assert _overrides(argv)["sandbox_mode"] == '"workspace-write"'


def test_parse_echo_fixture():
    events = _parse("codex-echo.jsonl")
    assert events == [
        Started("00000000-0000-4000-8000-000000000001", None),
        ToolCall("item_0", "command_execution", {"command": "/bin/bash -lc 'echo metabot-probe'"}),
        ToolResult("item_0", {"output": "metabot-probe\n", "exit_code": 0}, True),
        TextEvent("metabot-probe"),
        Usage(
            {
                "input_tokens": 32268,
                "cached_input_tokens": 27392,
                "cache_write_input_tokens": 0,
                "output_tokens": 82,
                "reasoning_output_tokens": 36,
            }
        ),
        Final("metabot-probe", "00000000-0000-4000-8000-000000000001"),
    ]


def test_parse_file_and_mcp():
    events = _parse("codex-file-and-mcp.jsonl")
    rec = StepRecorder("codex", skip_tools=CodexEngine.skip_tools)
    for e in events:
        rec.on_event(e)
    steps = rec.steps()
    assert [s["tool"] for s in steps] == ["codex/command_execution", "codex/file_change"]
    assert steps[0]["success"] is False
    assert steps[0]["output"] == {"output": "1 failed\n", "exit_code": 2}
    assert steps[1]["args"] == {"changes": [{"path": "/work/src/app.py", "kind": "update"}]}
    assert steps[1]["success"] is True
    texts = [e.text for e in events if isinstance(e, TextEvent)]
    assert any("hologram" in t for t in texts)  # unknown item: shown, never dropped
    assert not any("Planning" in t for t in texts)  # reasoning is not the answer
    assert events[-1] == Final(
        "Fixed app.py; one test still fails.", "00000000-0000-4000-8000-000000000002"
    )


def test_parse_turn_failed():
    events = _parse("codex-turn-failed.jsonl")
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors and "stream disconnected" in errors[0].message
    assert isinstance(events[-1], Final)


def test_notices_never_fail_a_turn():
    # Live 2026-10-11: a skills-budget notice arrived as an error and failed a good run.
    events = _parse("codex-notices.jsonl")
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.text for e in events if isinstance(e, Notice)] == [
        "codex: Reconnecting... 1/5",
        "codex: Skill descriptions were shortened to fit the skills context budget.",
    ]
    assert events[-1] == Final("metabot-live", "00000000-0000-4000-8000-000000000003")


@pytest.mark.parametrize("ok_status", ["completed"])
def test_mcp_call_to_another_server_is_a_step(ok_status):
    engine = CodexEngine()
    item = {
        "id": "i9",
        "type": "mcp_tool_call",
        "server": "docs",
        "tool": "search",
        "arguments": {"q": "x"},
        "result": {"content": []},
        "error": None,
        "status": ok_status,
    }
    events = engine.parse_line(json.dumps({"type": "item.completed", "item": item}), {})
    assert events[0] == ToolCall("i9", "mcp/docs/search", {"q": "x"})
    assert isinstance(events[1], ToolResult) and events[1].ok is True


async def test_runs_through_the_engine_base(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-echo.jsonl"))
    engine = wrap(CodexEngine)
    async with contextlib.aclosing(run_cli(engine, EngineJob("say hi", tmp_path))) as stream:
        events = [e async for e in stream]
    assert isinstance(events[0], Started)
    assert events[-1] == Final("metabot-probe", "00000000-0000-4000-8000-000000000001")
