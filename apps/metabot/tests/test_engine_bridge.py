"""Bridges: an engine a developer adds, as a program that speaks metabot.bridge/1 on stdout."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import ConfigError, load_config
from metabot.engines import engine_names, get_engine, known_engine
from metabot.engines.bridge import BridgeEngine
from metabot.gate import Policy
from metabot.paths import run_path
from metabot.receipts import verify_file
from metabot.runner import run_job
from metabot.steps import MAY_HAVE_RUN

ECHO = r"""
import json, sys
job = json.load(open(sys.argv[1], encoding="utf-8"))
def say(**event):
    print(json.dumps(event), flush=True)
say(type="started", session_id="b-1", model=job["model"])
say(type="text", text="thinking")
say(type="tool_call", call_id="t1", tool="shell", args={"command": "ls"})
say(type="tool_result", call_id="t1", output="a.txt", ok=True)
say(type="tool_call", call_id="t2", tool="write", args={"path": "b.txt"})
say(type="usage", data={"prompt_tokens": 7, "completion_tokens": 3})
print("not json at all", flush=True)
say(type="final", text="echo: " + job["prompt"], session_id="b-1")
"""


def _bridge(home: Path, tmp_path: Path, name: str = "echo", **spec) -> Path:
    script = tmp_path / f"{name}.py"
    script.write_text(ECHO, encoding="utf-8")
    config = home / "config.json"
    data = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    data.setdefault("bridges", {})[name] = {"command": [sys.executable, str(script)], **spec}
    home.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data), encoding="utf-8")
    return script


def _journal(result) -> dict:
    return json.loads((run_path(result.run_id) / "journal.json").read_text(encoding="utf-8"))


async def _run(tmp_path, engine="echo", **kw):
    return await run_job(
        engine=get_engine(engine),
        prompt="hello",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        model=kw.pop("model", "m-1"),
        **kw,
    )


# ── the table ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bridges",
    [
        {"Echo": {"command": ["x"]}},  # not a name
        {"claude": {"command": ["x"]}},  # a built-in engine's name
        {"echo": {"command": "x"}},  # a command is a list of words
        {"echo": {"command": []}},
        {"echo": {"command": ["x"], "tools": "all"}},
        {"echo": {"command": ["x"], "read_only": True}},  # honored or no
        {"echo": {"command": ["x"], "models": ["--evil"]}},
        {"echo": {"command": ["x"], "colour": "red"}},
    ],
)
def test_a_bridge_table_that_is_wrong_does_not_read(home, bridges):
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"bridges": bridges}), encoding="utf-8")
    with pytest.raises(ConfigError, match="bridges"):
        load_config()


def test_a_bridge_is_an_engine_by_its_name(home, tmp_path):
    _bridge(home, tmp_path)
    assert known_engine("echo") and not known_engine("nope")
    assert engine_names()[:4] == ["claude", "codex", "cursor", "openai"]
    assert "echo" in engine_names()
    engine = get_engine("echo")
    assert isinstance(engine, BridgeEngine) and engine.name == "echo"
    assert engine.own_tools_gated is False
    with pytest.raises(ValueError, match="echo"):  # the choices name the bridges too
        get_engine("nope")


# ── a run ───────────────────────────────────────────────────────────────────


async def test_a_bridge_run_is_an_ordinary_run_with_a_receipt(home, tmp_path):
    script = _bridge(home, tmp_path)
    result = await _run(tmp_path)
    assert result.ok and result.final_text == "echo: hello"
    journal = _journal(result)
    meta = journal["meta"]
    assert meta["engine"] == "echo" and meta["model"] == "m-1"
    assert meta["bridge"]["name"] == "echo" and meta["bridge"]["source"] == "config"
    assert meta["bridge"]["command"] == [sys.executable, str(script)]
    assert len(meta["bridge"]["digest"]) == 64  # the program it ran, by its bytes
    steps = {s["call_id"]: s for s in journal["steps"]}
    assert steps["t1"]["output"] == "a.txt"
    assert steps["t2"]["output"] == MAY_HAVE_RUN  # reported, never answered: it may have run


async def test_a_bridge_in_a_folder_whose_name_is_not_utf_8_is_run_and_signed(home, tmp_path):
    folder = tmp_path / os.fsdecode(b"caf\xe9")  # as a plugin's is, in a home of such a name
    folder.mkdir()
    script = _bridge(home, folder)
    result = await _run(tmp_path)
    # before: the program ran, and then the run stopped with a traceback, its receipt unsigned
    assert result.ok and verify_file(result.receipt).valid
    assert _journal(result)["meta"]["bridge"]["command"] == [sys.executable, str(script)]


async def test_the_job_file_is_private_and_says_what_to_do(home, tmp_path):
    _bridge(home, tmp_path)
    result = await _run(tmp_path)
    job_file = run_path(result.run_id) / "bridge-job.json"
    assert stat.S_IMODE(job_file.stat().st_mode) == 0o600
    job = json.loads(job_file.read_text(encoding="utf-8"))
    assert job["format"] == "metabot.bridge/1"
    assert (job["prompt"], job["model"], job["cwd"]) == ("hello", "m-1", str(tmp_path))
    assert job["read_only"] is False and job["switchboard"] is None


@pytest.mark.parametrize(("tools", "given"), [("own", False), ("switchboard", True)])
async def test_only_a_switchboard_bridge_is_given_the_switchboard(home, tmp_path, tools, given):
    _bridge(home, tmp_path, tools=tools)
    result = await _run(tmp_path, switchboard=True)
    job = json.loads((run_path(result.run_id) / "bridge-job.json").read_text(encoding="utf-8"))
    assert isinstance(job["switchboard"], list) is given
    if given:
        assert "mcp" in job["switchboard"]  # the command that serves it


async def test_a_bridge_says_once_that_its_own_tools_are_not_gated(home, tmp_path):
    _bridge(home, tmp_path)
    seen = []
    await _run(tmp_path, on_event=seen.append)
    notices = [e.text for e in seen if type(e).__name__ == "Notice"]
    assert sum("not gated by MetaBot" in n for n in notices) == 1


async def test_a_read_only_job_never_reaches_a_bridge_that_does_not_keep_to_it(home, tmp_path):
    _bridge(home, tmp_path)
    result = await _run(tmp_path, read_only=True)
    assert not result.ok and "does not keep to read-only" in str(result.error)
    assert not (tmp_path / "b.txt").exists()


async def test_a_bridge_that_keeps_to_read_only_is_told_so(home, tmp_path):
    _bridge(home, tmp_path, read_only="honored")
    result = await _run(tmp_path, read_only=True)
    assert result.ok
    job_file = run_path(result.run_id) / "bridge-job.json"
    assert json.loads(job_file.read_text(encoding="utf-8"))["read_only"] is True


async def test_a_bridge_that_is_not_installed_fails_and_says_so(home, tmp_path):
    home.mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps({"bridges": {"gone": {"command": [str(tmp_path / "no-such-program")]}}}),
        encoding="utf-8",
    )
    result = await _run(tmp_path, engine="gone")
    assert not result.ok and "not found" in str(result.error)


# ── where engines are named ─────────────────────────────────────────────────


def test_ask_runs_on_a_bridge_from_the_command_line(home, tmp_path, capsys):
    _bridge(home, tmp_path)
    assert main(["ask", "-e", "echo", "-m", "m-1", "--cwd", str(tmp_path), "hello"]) == 0
    assert capsys.readouterr().out == "echo: hello\n"


def test_engines_lists_a_bridge_and_its_command(home, tmp_path, capsys):
    script = _bridge(home, tmp_path)
    assert main(["engines"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if "echo" in x)
    assert "bridge" in line and str(script) in line


def test_an_unknown_engine_is_refused_with_the_choices(home, tmp_path, capsys):
    _bridge(home, tmp_path)
    assert main(["ask", "-e", "nope", "--cwd", str(tmp_path), "hello"]) == 2
    err = capsys.readouterr().err
    assert "unknown engine 'nope'" in err and "echo" in err


def test_an_office_desk_may_run_on_a_bridge(home, tmp_path):
    from metabot.office import desk_problem, load_office
    from test_office_run import _repo

    _bridge(home, tmp_path)
    desks = {
        "build": {"role": "roles/build.md", "harness": "custom", "command": "x", "engine": "echo"},
        "check": {
            "role": "roles/check.md", "harness": "any", "engine": "echo", "read_only": True,
        },
    }  # fmt: skip
    office = load_office(_repo(tmp_path, desks))
    assert desk_problem(office.desks["build"]) is None
    assert "does not keep to read-only" in str(desk_problem(office.desks["check"]))


def test_a_routine_on_a_bridge_needs_one_that_keeps_to_read_only(home, tmp_path):
    from metabot.connections import ConnectionStore
    from metabot.routines import RoutineError, RoutineSpec, check_routine

    _bridge(home, tmp_path)
    spec = RoutineSpec(
        name="daily", goal="say hi", engine="echo", model=None, cwd=str(tmp_path),
        connections=[], scope_allow=[], criteria={}, cron="0 9 * * *", tz="UTC",
    )  # fmt: skip
    with pytest.raises(RoutineError, match="read-only"):
        check_routine(spec, ConnectionStore(), lambda conn: [])
    _bridge(home, tmp_path, read_only="honored")
    check_routine(spec, ConnectionStore(), lambda conn: [])
