from __future__ import annotations

import contextlib
import json
import shlex

from conftest import FIXTURES
from fake_engines import wrap
from metabot.engines.base import EngineJob, run_cli
from metabot.engines.claude import ClaudeEngine
from metabot.events import ErrorEvent, Final, Started, TextEvent, ToolCall, ToolResult, Usage
from metabot.steps import StepRecorder


def _parse(name: str):
    engine = ClaudeEngine()
    state: dict = {}
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(engine.parse_line(line, state))
    return events


def test_argv_minimal(tmp_path):
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompts",
        "none",
        "--strict-mcp-config",
        "--",
        "hi",
    ]


def test_no_other_mcp_server_loads_without_a_switchboard(tmp_path):
    # A server from the person's config or the project's .mcp.json would be
    # gated by its tool names alone, and a name is whatever its author chose.
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path, plugin_dirs=[tmp_path]))
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_argv_full(tmp_path):
    hook = ["/usr/bin/python3", "-m", "metabot", "hook", "--run-dir", "/runs/a b"]
    job = EngineJob(
        prompt="-starts with a dash",
        cwd=tmp_path,
        model="claude-haiku-5-5",
        switchboard=["/usr/bin/python3", "-m", "metabot", "mcp", "serve", "--run-dir", "/r"],
        plugin_dirs=[tmp_path / "p1", tmp_path / "p2"],
        gate_hook=hook,
        resume="sess-1",
    )
    argv = ClaudeEngine().argv(job)
    assert argv[-2:] == ["--", "-starts with a dash"]
    assert argv.count("--") == 1
    i = argv.index("--mcp-config")
    assert json.loads(argv[i + 1]) == {
        "mcpServers": {
            "metabot": {
                "command": "/usr/bin/python3",
                "args": ["-m", "metabot", "mcp", "serve", "--run-dir", "/r"],
            }
        }
    }
    assert argv[i + 2] == "--strict-mcp-config"
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert [argv[j + 1] for j, a in enumerate(argv) if a == "--plugin-dir"] == [
        str(tmp_path / "p1"),
        str(tmp_path / "p2"),
    ]
    assert argv[argv.index("--resume") + 1] == "sess-1"
    settings = json.loads(argv[argv.index("--settings") + 1])
    [entry] = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    [h] = entry["hooks"]
    assert h["type"] == "command"
    assert shlex.split(h["command"]) == hook
    assert h["timeout"] >= 600
    assert "--permission-mode" not in argv


def test_argv_read_only(tmp_path):
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path, read_only=True))
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_argv_never_skips_permissions(tmp_path):
    job = EngineJob(prompt="hi", cwd=tmp_path, read_only=False, gate_hook=["x"])
    argv = ClaudeEngine().argv(job)
    assert not any("dangerously" in a or a == "bypassPermissions" for a in argv)


def test_parse_echo_fixture():
    events = [e for e in _parse("claude-echo.jsonl") if not isinstance(e, Usage)]
    assert events == [
        Started("00000000-0000-4000-8000-000000000001", "claude-haiku-5-5"),
        ToolCall(
            "toolu_0001",
            "Bash",
            {"command": "echo metabot-probe", "description": "Print probe string via echo"},
        ),
        ToolResult("toolu_0001", "metabot-probe", True),
        TextEvent("metabot-probe"),
        Final("metabot-probe", "00000000-0000-4000-8000-000000000001"),
    ]


def test_parse_mcp_and_error():
    events = _parse("claude-mcp-and-error.jsonl")
    rec = StepRecorder("claude-code", skip_tools=ClaudeEngine.skip_tools)
    for e in events:
        rec.on_event(e)
    [step] = rec.steps()
    assert step["tool"] == "claude-code/Bash"
    assert step["success"] is False
    assert step["output"] == "Exit code 1\n1 failed"
    texts = [e.text for e in events if isinstance(e, TextEvent)]
    assert "a newer CLI said something" in texts
    assert not any("someone else's hook" in t for t in texts)
    assert not any("let me look" in t for t in texts)
    [usage] = [e for e in events if isinstance(e, Usage)]
    assert usage.data["total_cost_usd"] == 0.01


def test_parse_hook_deny_fixture():
    events = _parse("claude-hook-deny.jsonl")
    rec = StepRecorder("claude-code")
    for e in events:
        rec.on_event(e)
    steps = rec.steps()
    assert [s["success"] for s in steps] == [True, False]
    assert steps[1]["args"]["command"] == "rm -f /work/nothing-here.txt"
    assert "hook error" in steps[1]["output"]
    assert not any("another plugin" in e.text for e in events if isinstance(e, TextEvent)), (
        "hook output from other plugins must never surface"
    )


def test_error_result_is_an_error_event():
    events = _parse("claude-error-result.jsonl")
    [err] = [e for e in events if isinstance(e, ErrorEvent)]
    assert "error_max_turns" in err.message
    assert isinstance(events[-1], Final)


async def test_runs_through_the_engine_base(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    engine = wrap(ClaudeEngine)
    async with contextlib.aclosing(run_cli(engine, EngineJob("say hi", tmp_path))) as stream:
        events = [e async for e in stream]
    assert isinstance(events[0], Started)
    assert isinstance(events[-1], Final)
    assert json.loads(out.read_text())["argv"][-2:] == ["--", "say hi"]
