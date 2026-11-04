"""Each engine CLI runs with the instructions of the folders above its project hidden."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from metabot import sandbox
from metabot.approvals import DenyAll
from metabot.config import Config
from metabot.engines import get_engine
from metabot.engines.base import CliEngine, EngineJob, run_cli
from metabot.engines.bridge import BridgeEngine
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.engines.cursor import CursorEngine
from metabot.events import ErrorEvent, Event, TextEvent
from metabot.gate import Policy
from metabot.receipts import verdicts_of
from metabot.runner import run_job

FIND_BWRAP = sandbox.find_bwrap  # the real one: each test gets a stand-in (conftest)
BWRAP = FIND_BWRAP()  # this machine's, checked to work; None skips the real runs


def _tree(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"rules from {rel}\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return done.stdout


def _repo(folder: Path) -> Path:
    """``folder`` as a git repository, with what it holds committed."""
    folder.mkdir(parents=True, exist_ok=True)
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "config", "user.name", "MetaBot Test")
    _git(folder, "config", "user.email", "metabot-test@example.invalid")
    _git(folder, "commit", "-q", "--allow-empty", "-m", "first")
    return folder


@pytest.fixture
def outer(tmp_path: Path) -> Path:
    """A folder of another project's instructions, holding the person's project."""
    root = tmp_path / "outer"
    _tree(root, "AGENTS.md", "CLAUDE.md", ".cursor/rules/a.mdc", ".agents/skills/s/SKILL.md")
    _tree(root, "proj/AGENTS.md", "proj/.cursor/rules/mine.mdc", "proj/src/")
    _repo(root / "proj")
    return root


def _shown(paths, root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in paths)


# ── what is found ───────────────────────────────────────────────────────────


def test_the_instructions_above_the_project_are_found_and_the_projects_own_are_not(outer):
    found = sandbox.found_above(outer / "proj" / "src")
    assert _shown(found, outer) == [".agents/skills", ".cursor/rules", "AGENTS.md", "CLAUDE.md"]


def test_without_a_repository_the_run_folder_is_the_project(outer):
    subprocess.run(["rm", "-rf", str(outer / "proj" / ".git")], check=True)
    found = sandbox.found_above(outer / "proj" / "src")
    assert "proj/AGENTS.md" in _shown(found, outer)  # above the folder the run was given
    assert "proj/src" not in _shown(found, outer)


def test_a_folder_that_holds_the_project_is_never_hidden(tmp_path):
    _tree(tmp_path, ".agents/skills/mine/SKILL.md", "AGENTS.md")
    _repo(tmp_path / ".agents" / "skills" / "mine")
    found = sandbox.found_above(tmp_path / ".agents" / "skills" / "mine")
    assert _shown(found, tmp_path) == ["AGENTS.md"]


def test_a_folder_that_holds_the_project_shows_nothing_else_it_holds(tmp_path):
    home = sandbox.user_home()
    _tree(home, ".claude/skills/mine/SKILL.md", ".claude/skills/other/SKILL.md")
    _tree(home, ".claude/skills/notes.md", ".claude/skills/mine/sub/")
    _repo(home / ".claude" / "skills" / "mine")
    plan = sandbox.plan_for(CursorEngine(), home / ".claude/skills/mine/sub", enabled=False)
    shown = _shown(plan.hidden, home)
    assert ".claude/skills/other" in shown and ".claude/skills/notes.md" in shown
    assert not [s for s in shown if s == ".claude/skills" or s.startswith(".claude/skills/mine")]


def test_a_dot_git_that_holds_no_repository_makes_no_project(tmp_path):
    home = sandbox.user_home()
    _tree(home, ".git/info/exclude", "AGENTS.md", "Downloads/")  # git takes no repository here
    assert sandbox.project_root(home / "Downloads") == (home / "Downloads").resolve()
    assert _shown(sandbox.found_above(home / "Downloads"), home) == ["AGENTS.md"]


def test_the_home_folder_is_never_the_project(tmp_path):
    home = sandbox.user_home()
    _tree(home, "AGENTS.md", ".cursor/rules/x.mdc", "notes/")
    _repo(home)  # a home folder kept in git
    assert _shown(sandbox.found_above(home / "notes"), home) == [".cursor/rules", "AGENTS.md"]
    # a run in the home folder itself has no project, so its own folder's are hidden too
    assert _shown(sandbox.found_above(home), home) == [".cursor/rules", "AGENTS.md"]


def test_a_run_in_the_home_folder_hides_the_engines_own_rules(monkeypatch):
    home = sandbox.user_home()
    _tree(home, ".cursor/rules/x.mdc", ".claude/skills/s/SKILL.md")
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    plan = sandbox.plan_for(CursorEngine(), home, enabled=True)
    assert {".cursor/rules", ".claude/skills"} <= set(_shown(plan.hidden, home))
    assert plan.refused is None and plan.tool == "/usr/bin/bwrap"


def test_a_worktree_inside_its_own_repository_keeps_the_main_checkouts_rules(tmp_path):
    main = tmp_path / "main"
    _tree(main, "AGENTS.md", ".cursor/rules/local.mdc")
    _repo(main)
    worktree = main / ".claude" / "worktrees" / "wt"
    _git(main, "worktree", "add", "-q", "--detach", str(worktree))
    assert sandbox.found_above(worktree) == []


def test_a_path_through_a_link_hides_what_lies_along_the_path_as_given(tmp_path):
    home = sandbox.user_home()
    _tree(home, "AGENTS.md", "x/")
    _tree(tmp_path, "disk/metabot/desks/l/rung-1/")
    (home / ".metabot").symlink_to(tmp_path / "disk" / "metabot")
    rung = (
        home / ".metabot" / "desks" / "l" / "rung-1"
    )  # an engine walks up this path, not the disk's
    assert "AGENTS.md" in _shown(sandbox.found_above(rung), home)


def test_a_file_put_out_of_use_with_a_link_to_dev_null_is_left_alone(tmp_path):
    home = sandbox.user_home()
    _tree(home, "proj/")
    _repo(home / "proj")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").symlink_to(os.devnull)
    assert sandbox.found_above(home / "proj") == []


def test_a_path_inside_a_hidden_folder_is_hidden_with_it(tmp_path):
    home = sandbox.user_home()
    _tree(home, ".cursor/rules/global.mdc", "proj/")
    _repo(home / "proj")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").symlink_to(home / ".cursor" / "rules" / "global.mdc")
    plan = sandbox.plan_for(ClaudeEngine(), home / "proj", enabled=False)
    assert _shown(plan.hidden, home) == [".cursor/rules"]  # one bind: bwrap cannot nest them


def test_a_link_that_leads_into_the_project_is_never_hidden(outer):
    (outer / "CLAUDE.md").unlink()
    (outer / "CLAUDE.md").symlink_to(outer / "proj" / "AGENTS.md")
    assert "CLAUDE.md" not in _shown(sandbox.found_above(outer / "proj"), outer)


def test_each_engine_hides_its_own_user_rules(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    monkeypatch.setattr(sandbox, "user_home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _tree(home, ".cursor/rules/x.mdc", ".cursor/skills/s/SKILL.md", ".agents/skills/b/SKILL.md")
    _tree(home, ".codex/skills/k/SKILL.md", ".codex/plugins/cache/p/", ".codex/AGENTS.md")
    _tree(home, ".codex/auth.json", ".cursor/cli-config.json", ".cursor/skills-cursor/b/")
    _tree(home, ".cursor/plugins/cache/p/", ".cursor/hooks.json", ".claude/plugins/cache/q/")
    _tree(home, ".claude/settings.json", ".claude/.credentials.json", ".grok/skills/g/SKILL.md")
    _tree(home, ".codex/rules/default.rules")
    assert _shown(CursorEngine().user_instructions(), home) == [
        ".agents/skills",
        ".claude/plugins",
        ".claude/settings.json",
        ".codex/skills",
        ".cursor/hooks.json",
        ".cursor/plugins",
        ".cursor/rules",
        ".cursor/skills",
        ".grok/skills",
    ]  # never a login, and never cursor's own bundled skills
    assert _shown(CodexEngine(Config()).user_instructions(), home) == [
        ".agents/skills",
        ".codex/AGENTS.md",
        ".codex/plugins",
        ".codex/skills",
    ]  # never its login, and never its rules, which forbid commands rather than instruct
    off = CodexEngine(Config(codex_ignore_user_config=False))
    assert off.user_instructions() == []  # the person asked for their own codex settings


def test_claude_hides_the_auto_memory_it_keeps_for_the_project(tmp_path, claude_config):
    project = tmp_path / "my.proj"
    project.mkdir()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    memory = claude_config / "projects" / slug / "memory"
    memory.mkdir(parents=True)
    assert ClaudeEngine().user_instructions(project) == [memory]
    assert ClaudeEngine().user_instructions(tmp_path / "other") == []


def _memory(claude_config: Path, folder: Path) -> Path:
    memory = claude_config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder)) / "memory"
    memory.mkdir(parents=True)
    return memory


def test_claude_hides_the_memory_a_worktree_shares_with_its_main_checkout(tmp_path, claude_config):
    main = _repo(tmp_path / "main")
    worktree = tmp_path / "desks" / "rung-1"  # where a ladder rung or an office desk runs
    _git(main, "worktree", "add", "-q", "--detach", str(worktree))
    memory = _memory(claude_config, main.resolve())  # claude keys its memory by the main checkout
    assert memory in ClaudeEngine().user_instructions(worktree)


def test_a_worktree_finds_a_main_checkout_whose_name_is_not_utf_8(tmp_path, claude_config):
    main = _repo(tmp_path / os.fsdecode(b"main-\xe9"))  # git writes the name's bytes as they are
    worktree = tmp_path / "desks" / "rung-1"
    _git(main, "worktree", "add", "-q", "--detach", str(worktree))
    # before: the worktree's .git file was read with that byte replaced, which names no
    # folder, so the main checkout was lost, and with it claude's memory of the project
    assert sandbox.main_checkout(worktree) == main.resolve()
    assert sandbox.copies_main(worktree) == main.resolve()
    memory = _memory(claude_config, main.resolve())
    assert memory in ClaudeEngine().user_instructions(worktree)


def test_claude_finds_the_memory_of_a_folder_too_long_to_name_whole(tmp_path, claude_config):
    deep = tmp_path.joinpath(*[f"a-long-folder-name-{n}" for n in range(14)])
    deep.mkdir(parents=True)
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(deep))
    assert len(slug) > 255  # no one folder can have that name
    memory = (
        claude_config / "projects" / f"{slug[:200]}-1a2b3c" / "memory"
    )  # claude's own short name
    memory.mkdir(parents=True)
    assert ClaudeEngine().user_instructions(deep) == [memory]
    assert sandbox.plan_for(ClaudeEngine(), deep, enabled=False).hidden == (memory,)


# ── the plan ────────────────────────────────────────────────────────────────


def _started(plan: sandbox.SandboxPlan) -> sandbox.SandboxPlan:
    plan.started = True  # run_cli says so once the engine's process is there
    return plan


def test_with_bwrap_every_source_is_hidden_and_the_receipt_says_how_many(outer, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    plan = sandbox.plan_for(CodexEngine(Config()), outer / "proj", enabled=True)
    assert plan.tool == "/usr/bin/bwrap" and len(plan.hidden) == 4 and plan.refused is None
    assert plan.verdict() is None  # nothing has run yet
    assert _started(plan).verdict() == {
        "check": "sandbox",
        "result": "pass",
        "detail": "bwrap;hidden=4",
    }


def test_cursors_plugin_folder_is_hidden_also_before_cursor_makes_it(outer, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    plugins = sandbox.user_home() / ".cursor" / "plugins"
    assert not plugins.exists()  # cursor-agent makes it as it starts, and fills it
    plan = sandbox.plan_for(CursorEngine(), outer / "proj", enabled=True)
    assert plugins.is_dir() and plugins.resolve() in plan.hidden and len(plan.hidden) == 5


def test_cursor_never_runs_with_rules_above_it_left_in_view(outer, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: None)
    plan = sandbox.plan_for(CursorEngine(), outer / "proj", enabled=True)
    assert plan.refused is not None and "bwrap" in plan.refused and "sandbox" in plan.refused
    assert plan.verdict() == {"check": "sandbox", "result": "fail", "detail": "refused;unhidden=4"}
    others = sandbox.plan_for(CodexEngine(Config()), outer / "proj", enabled=True)
    assert others.refused is None  # it runs, and says what it could not hide
    assert _started(others).verdict() == {
        "check": "sandbox",
        "result": "warn",
        "detail": "none;unhidden=4",
    }
    assert "4" in (others.note() or "")


def test_without_bwrap_cursor_never_runs_unless_the_sandbox_is_off(tmp_path):
    # its account's plugins and rules come down as it starts, whatever is on disk
    plan = sandbox.plan_for(CursorEngine(), tmp_path, enabled=True)
    assert plan.refused is not None and '"sandbox": false' in plan.refused
    assert sandbox.plan_for(CursorEngine(), tmp_path, enabled=False).refused is None


def test_sandbox_off_runs_cursor_and_says_what_it_left_in_view(outer, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    plan = sandbox.plan_for(CursorEngine(), outer / "proj", enabled=False)
    assert plan.tool is None and plan.refused is None
    assert _started(plan).verdict()["detail"] == "off;unhidden=4"


def test_nothing_to_hide_needs_no_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: None)
    plan = sandbox.plan_for(ClaudeEngine(), tmp_path, enabled=True)
    assert plan.refused is None and plan.note() is None
    assert _started(plan).verdict() == {
        "check": "sandbox",
        "result": "pass",
        "detail": "none;hidden=0",
    }


def test_a_file_a_bridge_command_names_is_never_hidden(outer):
    command = [sys.executable, "-I", str(outer / "AGENTS.md")]
    plan = sandbox.plan_for(BridgeEngine("b", {"command": command}), outer / "proj", enabled=False)
    assert (outer / "AGENTS.md").resolve() not in plan.hidden and len(plan.hidden) == 3


def test_bwrap_installed_while_metabot_runs_is_used_from_the_next_run(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "bwrap"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: str(fake))
    assert FIND_BWRAP() is None  # it does not work yet
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert FIND_BWRAP() == str(fake)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert FIND_BWRAP() is None  # and once it is gone, it is gone


def test_the_files_the_sandbox_shows_are_named_whole(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "METABOT_HOME", "relative-home"
    )  # bwrap would look for it in the run's folder
    assert sandbox.blank_file().is_absolute() and sandbox.empty_dir().is_absolute()


def test_something_put_in_the_empty_folder_takes_one_more_folder_and_no_more(home):
    first = sandbox.empty_dir()
    (first / ".directory").write_text("x", encoding="utf-8")
    second = sandbox.empty_dir()
    assert second != first and not any(second.iterdir())
    assert sandbox.empty_dir() == second  # not a new one for every run


def test_the_wrapped_command_shows_each_folder_empty_and_each_file_blank(outer):
    hidden = (outer / ".cursor/rules", outer / "AGENTS.md")
    plan = sandbox.SandboxPlan(
        hidden=hidden, tool="/usr/bin/bwrap", blank=outer / "blank", empty=outer / "empty"
    )
    argv = plan.wrap(["cursor-agent", "-p", "--", "hi"])
    assert argv[:4] == ["/usr/bin/bwrap", "--dev-bind", "/", "/"]
    assert "--die-with-parent" not in argv  # bwrap would kill the engine the moment it stops
    assert argv[4:11] == [
        "--ro-bind",
        str(outer / "empty"),
        str(outer / ".cursor/rules"),
        "--ro-bind",
        str(outer / "blank"),
        str(outer / "AGENTS.md"),
        "--",
    ]
    assert sandbox.inner_argv(argv) == ["cursor-agent", "-p", "--", "hi"]
    assert sandbox.inner_argv(["cursor-agent", "--", "x"]) == ["cursor-agent", "--", "x"]


# ── for real ────────────────────────────────────────────────────────────────

needs_bwrap = pytest.mark.skipif(BWRAP is None, reason="bwrap does not work here")

READER = r"""
import json, os, sys
def look(p):
    if os.path.isdir(p):
        return sorted(os.listdir(p))
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None
seen = {p: look(p) for p in sys.argv[1:]}
try:
    open(os.path.join(sys.argv[2], "filled.mdc"), "w").write("x")
    seen["filled"] = True
except OSError:
    seen["filled"] = False
open("written.txt", "w").write("ok")
print(json.dumps(seen))
"""


@needs_bwrap
def test_a_wrapped_process_sees_the_sources_empty_and_the_project_as_it_is(
    outer, tmp_path, monkeypatch
):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: BWRAP)
    reader = tmp_path / "reader.py"
    reader.write_text(READER, encoding="utf-8")
    plan = sandbox.plan_for(CursorEngine(), outer / "proj", enabled=True)
    looked = [outer / "AGENTS.md", outer / ".cursor/rules", outer / "proj/AGENTS.md"]
    argv = plan.wrap([sys.executable, "-I", str(reader), *map(str, looked)])
    out = subprocess.run(argv, cwd=outer / "proj", capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    assert seen[str(looked[0])] == "" and seen[str(looked[1])] == []
    assert seen["filled"] is False  # a hidden folder cannot be filled again as the engine starts
    assert seen[str(looked[2])] == "rules from proj/AGENTS.md\n"
    assert (outer / "proj" / "written.txt").read_text() == "ok"  # the project is written as before
    assert (outer / "AGENTS.md").read_text() == "rules from AGENTS.md\n"  # and nothing is changed


@needs_bwrap
def test_a_link_into_a_hidden_folder_starts_the_engine_all_the_same(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: BWRAP)
    home = sandbox.user_home()
    _tree(home, ".cursor/rules/global.mdc", "proj/")
    _repo(home / "proj")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").symlink_to(home / ".cursor" / "rules" / "global.mdc")
    plan = sandbox.plan_for(ClaudeEngine(), home / "proj", enabled=True)
    done = subprocess.run(plan.wrap(["true"]), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


BRIDGE = r"""
import json, sys
job = json.load(open(sys.argv[-1], encoding="utf-8"))
text = open(job["prompt"], encoding="utf-8").read()  # the file the task names
print(json.dumps({"type": "final", "text": "saw: " + repr(text)}), flush=True)
"""


async def _bridge_run(home: Path, tmp_path: Path, outer: Path):
    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE, encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", str(script)]
    bridges = {"reader": {"command": command}}
    (home / "config.json").write_text(json.dumps({"bridges": bridges}), encoding="utf-8")
    return await run_job(
        engine=get_engine("reader"),
        prompt=str(outer / "AGENTS.md"),
        cwd=outer / "proj",
        policy=Policy(),
        approver=DenyAll(),
    )


@needs_bwrap
async def test_a_run_sees_the_instructions_above_it_blank_and_its_receipt_signs_it(
    home, tmp_path, outer, monkeypatch
):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: BWRAP)
    result = await _bridge_run(home, tmp_path, outer)
    assert result.ok, result.error
    assert result.final_text == "saw: ''"
    assert {"check": "sandbox", "result": "pass", "detail": "bwrap;hidden=4"} in verdicts_of(
        result.receipt
    )


async def test_without_bwrap_a_run_says_so_and_its_receipt_warns(home, tmp_path, outer):
    result = await _bridge_run(home, tmp_path, outer)  # the tests' default: no bwrap
    assert result.final_text == "saw: 'rules from AGENTS.md\\n'"
    verdicts = verdicts_of(result.receipt)
    assert {"check": "sandbox", "result": "warn", "detail": "none;unhidden=4"} in verdicts


async def test_a_cursor_run_with_rules_above_it_and_no_sandbox_is_refused_and_sealed(
    home, tmp_path, outer
):
    result = await run_job(
        engine=CursorEngine(),
        prompt="hi",
        cwd=outer / "proj",
        policy=Policy(),
        approver=DenyAll(),
    )
    assert not result.ok and "bwrap" in (result.error or "")
    assert result.receipt is not None and Path(result.receipt).is_file()
    verdicts = verdicts_of(result.receipt)  # never the verdict of a run that went ahead
    assert {"check": "sandbox", "result": "fail", "detail": "refused;unhidden=4"} in verdicts


async def test_a_run_whose_engine_never_starts_signs_nothing_of_a_sandbox(
    home, tmp_path, outer, monkeypatch
):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    home.mkdir(parents=True, exist_ok=True)
    gone = tmp_path / "no-such-program"
    bridges = {"gone": {"command": [str(gone)]}}
    (home / "config.json").write_text(json.dumps({"bridges": bridges}), encoding="utf-8")
    result = await run_job(
        engine=get_engine("gone"),
        prompt="hi",
        cwd=outer / "proj",
        policy=Policy(),
        approver=DenyAll(),
    )
    assert f"{gone} not found; install it or check PATH" in (result.error or "")
    assert [v for v in verdicts_of(result.receipt) if v.get("check") == "sandbox"] == []


async def test_a_plan_that_cannot_be_made_stops_the_run_before_it_takes_a_folder(
    home, outer, monkeypatch
):
    def cannot(*args, **kwargs):
        raise OSError("a folder that cannot be read")

    monkeypatch.setattr("metabot.runner.plan_for", cannot)
    with pytest.raises(OSError):
        await run_job(
            engine=CodexEngine(Config()),
            prompt="hi",
            cwd=outer / "proj",
            policy=Policy(),
            approver=DenyAll(),
        )
    runs = home / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []  # no run folder holding a policy alone


class _Stops(CliEngine):
    """An engine that prints one line, then takes a moment to stop when told to."""

    name = "stops"
    binary = sys.executable
    tool_prefix = "stops"

    def __init__(self, script: Path, said: Path) -> None:
        self.script, self.said = script, said

    def argv(self, job: EngineJob) -> list[str]:
        return [sys.executable, "-I", str(self.script), str(self.said)]

    def parse_event(self, obj: dict, state: dict) -> list[Event]:
        return [TextEvent(str(obj.get("text")))]


STOPS = r"""
import json, signal, sys, time
def stop(signum, frame):
    if sys.argv[2:] == ["deaf"]:
        return
    time.sleep(0.5)  # what an engine saves as it stops
    open(sys.argv[1], "w").write("saved")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({"type": "text", "text": "hi"}), flush=True)
for _ in range(600):
    time.sleep(0.1)
"""


async def _stop_after_the_first_line(engine: CliEngine, job: EngineJob) -> None:
    async with contextlib.aclosing(run_cli(engine, job)) as stream:
        async for event in stream:
            assert event == TextEvent("hi")
            break


@needs_bwrap
async def test_an_engine_stopped_in_the_sandbox_has_its_time_to_stop(outer, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: BWRAP)
    script = tmp_path / "stops.py"
    script.write_text(STOPS, encoding="utf-8")
    engine = _Stops(script, tmp_path / "said.txt")
    plan = sandbox.plan_for(engine, outer / "proj", enabled=True)
    assert plan.tool is not None
    await _stop_after_the_first_line(engine, EngineJob("x", outer / "proj", sandbox=plan))
    assert (tmp_path / "said.txt").read_text() == "saved"  # it was not killed as bwrap went


@needs_bwrap
async def test_an_engine_that_will_not_stop_in_the_sandbox_is_killed(outer, tmp_path, monkeypatch):
    from metabot.engines import base

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: BWRAP)
    monkeypatch.setattr(base, "KILL_GRACE", 0.5)
    script = tmp_path / "stops.py"
    script.write_text(STOPS, encoding="utf-8")

    class Deaf(_Stops):
        def argv(self, job: EngineJob) -> list[str]:
            return [*super().argv(job), "deaf"]

    engine = Deaf(script, tmp_path / "said.txt")
    plan = sandbox.plan_for(engine, outer / "proj", enabled=True)
    job = EngineJob("x", outer / "proj", sandbox=plan)
    await asyncio.wait_for(_stop_after_the_first_line(engine, job), 10)
    for _ in range(40):  # what was killed is gone once its new parent has waited for it
        found = subprocess.run(["pgrep", "-f", str(script)], capture_output=True, text=True)
        if not found.stdout.strip():
            break
        await asyncio.sleep(0.05)
    assert found.stdout.strip() == ""  # nothing of it is left running


async def test_a_missing_engine_in_the_sandbox_is_named_and_so_is_a_missing_bwrap(tmp_path):
    class Gone(_Stops):
        def argv(self, job: EngineJob) -> list[str]:
            return [str(tmp_path / "no-such-cli"), "-p"]

    def plan(tool: str) -> sandbox.SandboxPlan:
        return sandbox.SandboxPlan(
            hidden=(tmp_path,), tool=tool, blank=tmp_path / "b", empty=tmp_path
        )

    engine = Gone(tmp_path / "s.py", tmp_path / "said.txt")
    async with contextlib.aclosing(
        run_cli(engine, EngineJob("x", tmp_path, sandbox=plan(BWRAP or "/usr/bin/bwrap")))
    ) as stream:
        [err] = [e async for e in stream]
    assert isinstance(err, ErrorEvent) and err.message.endswith(
        f"{tmp_path / 'no-such-cli'} not found; install it or check PATH"
    )
    gone_bwrap = str(tmp_path / "gone" / "bwrap")
    async with contextlib.aclosing(
        run_cli(
            _Stops(tmp_path / "s.py", tmp_path / "said.txt"),
            EngineJob("x", tmp_path, sandbox=plan(gone_bwrap)),
        )
    ) as stream:
        [err] = [e async for e in stream]
    assert isinstance(err, ErrorEvent) and gone_bwrap in err.message


def test_the_tests_guard_sees_the_engine_a_wrapped_command_starts(tmp_path):
    from conftest import _engine_started

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\n")
    (bin_dir / "claude").chmod(0o755)
    wrapped = ["/usr/bin/bwrap", "--dev-bind", "/", "/", "--", "claude", "-p"]
    call = {"args": wrapped, "shell": False, "executable": None, "env": {"PATH": str(bin_dir)}}
    assert _engine_started(call, ()) == str(bin_dir / "claude")


def test_the_sandbox_is_a_setting_config_set_turns_off(capsys):
    from metabot.cli import main
    from metabot.config import load_config

    assert load_config().sandbox is True
    assert main(["config", "set", "sandbox", "off"]) == 0
    assert load_config().sandbox is False
    capsys.readouterr()
    assert main(["config", "show"]) == 0
    assert "sandbox" in capsys.readouterr().out
