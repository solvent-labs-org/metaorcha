from __future__ import annotations

import contextlib
import json
import os
import shlex
import threading
from pathlib import Path

import pytest

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
        "--setting-sources",
        "",
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


@pytest.mark.parametrize(
    "job",
    [
        {},
        {"read_only": True},
        {"resume": "sess-1"},
        {"model": "m", "gate_hook": ["gate"], "switchboard": ["sb"], "plugin_dirs": [Path("/p")]},
    ],
)
def test_no_settings_file_loads(tmp_path, job):
    # A repo's committed .claude settings can run hooks and a key helper, and
    # their env can send the model calls, with the login, somewhere else.
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path, **job))
    assert argv.count("--setting-sources") == 1
    i = argv.index("--setting-sources")
    assert argv[i + 1] == ""
    assert i < argv.index("--")


def _write_settings(folder: Path, data: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def _settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


TELEMETRY = (
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
PERSON = {
    "model": "claude-sonnet-5-5",
    "effortLevel": "low",  # read differently from a flag, so it is left out
    "modelSettings": {
        "claude-sonnet-5-5": {"effortLevel": "xhigh", "aSettingOfTheirs": "x"},
        "claude-opus-5-5": {"effortLevel": "high"},
        "claude-haiku-5-5": {"aSettingOfTheirs": "x"},
    },
    "env": {
        **{name: "1" for name in TELEMETRY},
        "ANTHROPIC_API_KEY": "a-key-that-stays-in-the-file",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/elsewhere",
        "A_VARIABLE_OF_THEIRS": "a-variable-of-theirs",
    },
    "apiKeyHelper": "echo a-helper-that-never-runs",
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "a-hook-of-theirs"}]}]},
    "permissions": {
        "allow": ["Write(an-allow-rule-of-theirs)"],
        "ask": ["Bash(an-ask-rule-of-theirs)"],
        "deny": ["Write(//etc/**)", 7, "Read(~/.ssh/**)"],
        "defaultMode": "bypassPermissions",
    },
    "aSettingOfTheirs": "a-setting-of-theirs",
}


def test_the_person_keeps_their_named_choices_and_nothing_else(tmp_path, claude_config):
    _write_settings(claude_config, PERSON)
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert _settings(argv) == {
        "model": "claude-sonnet-5-5",
        "modelSettings": {
            "claude-sonnet-5-5": {"effortLevel": "xhigh"},
            "claude-opus-5-5": {"effortLevel": "high"},
        },
        "env": {name: "1" for name in TELEMETRY},
        "permissions": {"deny": ["Write(//etc/**)", "Read(~/.ssh/**)"]},
    }
    text = json.dumps(argv)
    for kept_out in (
        "a-key-that-stays",
        "/elsewhere",
        "a-variable-of-theirs",
        "a-helper",
        "a-hook-of",
        "an-allow-rule",
        "an-ask-rule",
        "bypassPermissions",
        "aSettingOfTheirs",
        "a-setting-of-theirs",
    ):
        assert kept_out not in text


def test_a_model_on_the_run_wins_over_the_persons(tmp_path, claude_config):
    _write_settings(claude_config, PERSON)
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path, model="claude-haiku-5-5"))
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    settings = _settings(argv)
    assert "model" not in settings
    assert settings["modelSettings"]["claude-opus-5-5"] == {"effortLevel": "high"}


def test_the_gate_hook_and_the_persons_choices_share_one_settings(tmp_path, claude_config):
    _write_settings(claude_config, {"permissions": {"deny": ["Bash(rm:*)"]}})
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path, gate_hook=["gate"]))
    assert argv.count("--settings") == 1
    settings = _settings(argv)
    assert settings["permissions"] == {"deny": ["Bash(rm:*)"]}
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "gate"


def test_without_a_config_folder_the_settings_come_from_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    _write_settings(tmp_path / "person" / ".claude", {"model": "from-home"})
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert _settings(argv) == {"model": "from-home"}


def test_with_cowork_plugins_the_cowork_settings_file_is_read(tmp_path, claude_config, monkeypatch):
    _write_settings(claude_config, {"model": "from-settings"})
    (claude_config / "cowork_settings.json").write_text(
        '{"model": "from-cowork"}', encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CODE_USE_COWORK_PLUGINS", "1")
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert _settings(argv) == {"model": "from-cowork"}


def test_the_repositorys_own_settings_are_never_carried(tmp_path):
    for name in ("settings.json", "settings.local.json"):
        _write_settings(tmp_path / ".claude", {"model": "from-the-repo"})
        (tmp_path / ".claude" / name).write_text(
            json.dumps({"model": "from-the-repo", "permissions": {"deny": ["Write"]}}),
            encoding="utf-8",
        )
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert "--settings" not in argv
    assert "from-the-repo" not in json.dumps(argv)


@pytest.mark.parametrize("raw", ["cfg", "", "~/cfg", "~nosuchuser42/cfg", "./cfg"])
def test_a_config_folder_that_is_not_absolute_carries_nothing(tmp_path, monkeypatch, raw):
    # The engine would look for it inside the run's folder, which the
    # repository controls; it expands no "~".
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", raw)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_settings(tmp_path / "cfg", {"model": "from-the-repo"})
    _write_settings(tmp_path, {"model": "from-the-repo"})
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert "--settings" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param("a folder", id="folder"),
        pytest.param(b"{not json", id="bad-json"),
        pytest.param(b"\xff\xfe not text", id="not-utf8"),
        pytest.param(b"[1, 2]", id="a-list"),
        pytest.param(
            b'{"model": 7, "modelSettings": {"m": {"effortLevel": 1}, "n": "high"},'
            b' "env": {"DISABLE_TELEMETRY": 1}, "permissions": {"deny": [1, null]}}',
            id="not-strings",
        ),
        pytest.param(
            b'{"modelSettings": ["m"], "env": ["DISABLE_TELEMETRY"],'
            b' "permissions": {"deny": "Write"}}',
            id="wrong-shapes",
        ),
        pytest.param(b'{"permissions": ["deny"]}', id="permissions-a-list"),
        pytest.param(
            b'{"model": "past the cap", "pad": "' + b"x" * (1 << 20) + b'"}', id="past-the-cap"
        ),
    ],
)
def test_settings_that_cannot_be_read_carry_nothing(tmp_path, claude_config, content):
    claude_config.mkdir()
    if content == "a folder":
        (claude_config / "settings.json").mkdir()
    elif isinstance(content, bytes):
        (claude_config / "settings.json").write_bytes(content)
    argv = ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert "--settings" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_a_settings_path_that_is_not_a_file_is_never_opened(tmp_path, claude_config):
    # opening a FIFO waits for a writer; a run must not wait on one
    claude_config.mkdir()
    fifo = claude_config / "settings.json"
    os.mkfifo(fifo)
    done: list[list[str]] = []
    worker = threading.Thread(
        target=lambda: done.append(ClaudeEngine().argv(EngineJob(prompt="hi", cwd=tmp_path))),
        daemon=True,
    )
    worker.start()
    worker.join(5)
    if worker.is_alive():  # let the stuck open go, then fail
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(5)
        pytest.fail("argv() opened the FIFO and waited")
    assert "--settings" not in done[0]


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
