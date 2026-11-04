"""metabot engines --manifest: each engine, with its read-only mode, its models, what the
sandbox does with the instructions around a folder, and what still reaches it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from metabot import sandbox
from metabot.cli import main
from metabot.config import Config, save_config


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A folder with another project's instructions in the folder above it."""
    (tmp_path / "AGENTS.md").write_text("rules from above\n", encoding="utf-8")
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


def _settings(home: Path, **settings: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(settings), encoding="utf-8")


def _manifest(capsys, folder: Path) -> dict[str, list[str]]:
    """Each engine's lines, by its name, with their spaces made single."""
    assert main(["engines", "--manifest", "--cwd", str(folder)]) == 0
    out: dict[str, list[str]] = {}
    name = ""
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("    "):
            out[name].append(" ".join(line.split()))
        else:
            name = line[2:].split()[0]
            out[name] = []
    return out


def test_with_bwrap_each_engine_says_how_many_it_hides_and_cursor_its_plugin_folder_too(
    proj, capsys, monkeypatch
):
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    shown = _manifest(capsys, proj)
    assert "sandbox hides 2 here, with bwrap" in shown["claude"]
    assert "sandbox hides 2 here, with bwrap" in shown["codex"]
    # a cursor run makes its plugin folder first, so what the account sends down is hidden too
    assert "sandbox hides 3 here, with bwrap" in shown["cursor"]
    assert not (sandbox.user_home() / ".cursor" / "plugins").exists()  # a view makes nothing
    assert "sandbox none needed: it reads no instructions of its own" in shown["openai"]


def test_each_engine_says_how_it_reads_without_writing_and_what_still_reaches_it(proj, capsys):
    shown = _manifest(capsys, proj)
    assert "read-only plan mode" in shown["claude"]
    assert "read-only its read-only sandbox" in shown["codex"]
    assert "read-only ask mode" in shown["cursor"]
    assert "read-only no write tools offered" in shown["openai"]
    assert "also sees its built-in skills" in shown["claude"]
    assert (
        "also sees its bundled skills, the user rules of the account it signs in with, and, "
        "with a connection on, the MCP servers in your ~/.cursor/mcp.json, outside the gate"
        in shown["cursor"]
    )
    assert "also sees only what the run sends it" in shown["openai"]


def test_without_bwrap_cursor_refuses_and_the_others_leave_them_in_view(proj, capsys):
    shown = _manifest(capsys, proj)  # the tests' machine has no bwrap
    refused = "sandbox refuses here: bwrap does not work on this machine (2 to hide)"
    assert refused in shown["cursor"]
    assert "sandbox 2 left in view here: bwrap does not work on this machine" in shown["codex"]
    save_config(Config(sandbox=False))
    shown = _manifest(capsys, proj)
    assert "sandbox off in config.json: 2 left in view here" in shown["cursor"]


def test_a_folder_with_nothing_around_it_has_nothing_to_hide_but_cursor_still_refuses(
    tmp_path, capsys
):
    alone = tmp_path / "alone"
    alone.mkdir()
    shown = _manifest(capsys, alone)
    assert "sandbox nothing to hide here" in shown["claude"]
    assert "sandbox refuses here: bwrap does not work on this machine" in shown["cursor"]


def test_the_manifest_names_each_role_s_model_and_each_endpoint_s_tier(proj, capsys, home):
    _settings(
        home,
        roster={"fast": {"engine": "claude", "model": "haiku"}, "review": {"engine": "codex"}},
        endpoints={
            "box": {"base_url": "http://127.0.0.1:11434/v1", "model": "coder-7b"},
            "far": {"base_url": "https://models.example/v1", "model": "big-1", "tier": "paid"},
        },
    )
    shown = _manifest(capsys, proj)
    assert "models fast: haiku" in shown["claude"]
    assert "models review: its default" in shown["codex"]
    assert "models no role uses it" in shown["cursor"]
    default = Config().openai_model
    said = f"endpoints default: {default}, local; box: coder-7b, local; far: big-1, paid"
    assert said in shown["openai"]


def test_a_bridge_says_whether_it_keeps_to_read_only_and_what_it_offers(proj, capsys, home):
    _settings(
        home,
        bridges={
            "kept": {"command": ["/bin/echo"], "models": ["m-1"], "read_only": "honored"},
            "loose": {"command": ["/bin/echo"]},
        },
    )
    shown = _manifest(capsys, proj)
    assert "read-only its own, as it says (read_only: honored)" in shown["kept"]
    assert "models offers m-1; no role uses it" in shown["kept"]
    assert "read-only none: it never takes a read-only turn" in shown["loose"]
    assert "sandbox 2 left in view here: bwrap does not work on this machine" in shown["loose"]
    assert "also sees whatever its own program reads" in shown["loose"]


def test_the_folder_must_be_one_and_goes_with_the_manifest(tmp_path, capsys):
    assert main(["engines", "--manifest", "--cwd", str(tmp_path / "none")]) == 2
    assert "is not a directory" in capsys.readouterr().err
    assert main(["engines", "--cwd", str(tmp_path)]) == 2
    assert "--cwd goes with --manifest" in capsys.readouterr().err


def test_the_plain_list_is_as_it_was(capsys):
    assert main(["engines"]) == 0
    assert "read-only" not in capsys.readouterr().out


@pytest.mark.parametrize("bwrap", ["/usr/bin/bwrap", None])
@pytest.mark.parametrize("enabled", [True, False])
def test_what_the_view_foresees_is_what_a_run_s_plan_holds(proj, monkeypatch, bwrap, enabled):
    from metabot.engines.claude import ClaudeEngine
    from metabot.engines.codex import CodexEngine
    from metabot.engines.cursor import CursorEngine

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: bwrap)
    for engine in (ClaudeEngine(), CodexEngine(), CursorEngine()):
        said = sandbox.foresee(engine, proj, enabled=enabled)  # before the run makes anything
        plan = sandbox.plan_for(engine, proj, enabled=enabled)
        count = len(plan.hidden)
        if plan.refused is not None:
            assert said == f"refuses here: bwrap does not work on this machine ({count} to hide)"
        elif plan.off:
            assert said == f"off in config.json: {count} left in view here"
        elif plan.tool is not None:
            assert said == f"hides {count} here, with bwrap"
        else:
            assert said == f"{count} left in view here: bwrap does not work on this machine"


def test_a_plugin_bridge_whose_copy_changed_shows_that_a_run_refuses_it(proj, capsys, tmp_path):
    from metabot.plugins import enable
    from test_plugin_bridges import _installed

    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    assert "read-only none: it never takes a read-only turn" in _manifest(capsys, proj)["echo"]
    (plugin.root / "bin" / "echo.py").write_text("print('changed')\n", encoding="utf-8")
    shown = _manifest(capsys, proj)["echo"]
    assert len(shown) == 1
    assert shown[0].startswith("refuses ")
    assert "changed since it was installed" in shown[0]


# ── review fixes: what each line says is what a run does ───────────────────


def test_a_role_with_no_model_shows_the_one_config_json_sets_for_its_engine(proj, capsys, home):
    _settings(
        home,
        models={"claude": "opus-x"},
        roster={
            "build": {"engine": "claude"},
            "fast": {"engine": "claude", "model": "haiku"},
            "private": {"engine": "openai"},
            "review": {"engine": "codex"},
        },
    )
    shown = _manifest(capsys, proj)
    assert "models fast: haiku, build: opus-x (models in config.json)" in shown["claude"]
    assert "models review: its default" in shown["codex"]
    assert f"models private: {Config().openai_model}" in shown["openai"]


def test_a_copy_s_line_counts_what_a_desk_or_rung_copy_hides(tmp_path, capsys, monkeypatch):
    from metabot.engines.claude import ClaudeEngine
    from test_office_run import _git, _repo

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    user = sandbox.user_home()
    user.mkdir(parents=True)
    (user / "AGENTS.md").write_text("the person's own\n", encoding="utf-8")
    monkeypatch.setenv("METABOT_HOME", str(user / ".metabot"))  # where it is by default
    repo = _repo(tmp_path / "elsewhere")  # a project outside the home folder
    shown = _manifest(capsys, repo)
    assert "sandbox nothing to hide here" in shown["claude"]
    assert "in a copy hides 1 in a copy, with bwrap" in shown["claude"]
    copy = user / ".metabot" / "desks" / "o-1" / "build"  # where an office puts a desk
    _git(repo, "worktree", "add", "-q", "--detach", str(copy))
    assert len(sandbox.plan_for(ClaudeEngine(), copy, enabled=True).hidden) == 1


def test_a_copy_s_line_counts_the_memory_claude_keeps_for_the_project(
    tmp_path, capsys, monkeypatch, home, claude_config
):
    from metabot.engines.claude import ClaudeEngine
    from test_office_run import _git, _repo
    from test_sandbox import _memory

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    repo = _repo(tmp_path / "elsewhere")
    _memory(claude_config, repo.resolve())  # a copy's memory is its main checkout's
    shown = _manifest(capsys, repo)
    assert "sandbox hides 1 here, with bwrap" in shown["claude"]
    assert "in a copy hides 1 in a copy, with bwrap" in shown["claude"]
    copy = home / "desks" / "o-1" / "build"
    _git(repo, "worktree", "add", "-q", "--detach", str(copy))
    assert len(sandbox.plan_for(ClaudeEngine(), copy, enabled=True).hidden) == 1


def test_a_copy_s_line_walks_a_repository_that_holds_metabot_s_home(tmp_path, capsys, monkeypatch):
    from metabot.engines.codex import CodexEngine
    from test_office_run import _git, _repo

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    outer = tmp_path / "dotfiles"  # a repository of its own, with its own instructions
    outer.mkdir()
    _git(outer, "init", "-q")
    (outer / "AGENTS.md").write_text("the dotfiles' own\n", encoding="utf-8")
    (outer / ".cursor" / "rules").mkdir(parents=True)
    monkeypatch.setenv("METABOT_HOME", str(outer / ".metabot"))
    repo = _repo(tmp_path / "elsewhere")
    shown = _manifest(capsys, repo)
    assert "in a copy hides 2 in a copy, with bwrap" in shown["codex"]
    copy = outer / ".metabot" / "desks" / "o-1" / "build"  # a copy is a git root of its own
    _git(repo, "worktree", "add", "-q", "--detach", str(copy))
    assert len(sandbox.plan_for(CodexEngine(Config()), copy, enabled=True).hidden) == 2


def test_a_folder_outside_git_has_no_copy_s_line(proj, capsys):
    shown = _manifest(capsys, proj)  # an office needs git, and a ladder rung works in place
    assert not any(line.startswith("in a copy") for line in shown["claude"])


def test_a_folder_git_has_no_work_tree_for_has_no_copy_s_line(tmp_path, capsys):
    from test_office_run import _git, _repo

    holder = _repo(tmp_path)  # made bare: a folder that holds a repository, its trees beside it
    _git(holder, "config", "core.bare", "true")
    _git(holder / ".git", "worktree", "add", "-q", "--detach", str(holder / "main"))
    shown = _manifest(capsys, holder)  # an office refuses here, and a rung works in place
    assert not any(line.startswith("in a copy") for line in shown["claude"])
    shown = _manifest(capsys, holder / "main")
    assert any(line.startswith("in a copy") for line in shown["claude"])


def test_codex_with_the_person_s_settings_on_says_they_reach_it(proj, capsys, home):
    _settings(home, codex_ignore_user_config=False)
    shown = _manifest(capsys, proj)
    assert (  # a folder in your home has ~/.agents/skills above it, which the sandbox hides
        "also sees its built-in skills, your own codex home's config.toml, AGENTS.md, "
        "skills and plugins, and, in a folder outside your home folder, the skills in "
        "~/.agents/skills: codex_ignore_user_config is off"
    ) in shown["codex"]
    assert "also sees its built-in skills" in _settled(capsys, proj)["codex"]


def _settled(capsys, folder: Path) -> dict[str, list[str]]:
    save_config(Config())  # the setting back on, as it ships
    return _manifest(capsys, folder)


def test_cursor_says_a_connection_lets_the_person_s_own_servers_run(proj, capsys):
    said = _manifest(capsys, proj)["cursor"]
    assert (
        "also sees its bundled skills, the user rules of the account it signs in with, and, "
        "with a connection on, the MCP servers in your ~/.cursor/mcp.json, outside the gate"
    ) in said


def test_a_plugin_that_no_longer_checks_out_is_said_to_stop_every_run(proj, capsys, tmp_path):
    from metabot.plugins import enable
    from test_plugin_bridges import _installed

    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    (plugin.view / "extra.txt").write_text("changed\n", encoding="utf-8")
    assert main(["engines", "--manifest", "--cwd", str(proj)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("plugins  every run that takes plugins refuses: tools@m's view")
    assert first.endswith("; --no-plugins runs without them")


def test_an_unreadable_file_in_a_plugin_s_copy_is_a_refusal_not_a_crash(proj, capsys, tmp_path):
    from metabot.plugins import enable
    from test_plugin_bridges import _installed

    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    locked = plugin.root / "bin" / "echo.py"
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("this user reads any file")
        shown = _manifest(capsys, proj)["echo"]
    finally:
        locked.chmod(0o755)
    assert len(shown) == 1 and shown[0].startswith("refuses cannot read tools@m's installed copy")


def test_a_folder_that_cannot_be_resolved_or_reached_is_not_a_directory(tmp_path, capsys):
    (tmp_path / "loop-a").symlink_to(tmp_path / "loop-b")
    (tmp_path / "loop-b").symlink_to(tmp_path / "loop-a")
    assert main(["engines", "--manifest", "--cwd", str(tmp_path / "loop-a")]) == 2
    assert "is not a directory" in capsys.readouterr().err
    shut = tmp_path / "shut"
    (shut / "inside").mkdir(parents=True)
    shut.chmod(0)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        assert main(["engines", "--manifest", "--cwd", str(shut / "inside")]) == 2
        assert "is not a directory" in capsys.readouterr().err
    finally:
        shut.chmod(0o755)


def test_a_folder_named_from_a_removed_folder_is_not_a_directory(tmp_path, capsys, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    for named in (".", "sub", ".."):
        assert main(["engines", "--manifest", "--cwd", named]) == 2
        assert "is not a directory" in capsys.readouterr().err


def _readonly_cursor(user: Path) -> None:
    (user / ".cursor").mkdir(parents=True)
    (user / ".cursor").chmod(0o555)


def _dangling_plugins(user: Path) -> None:
    (user / ".cursor").mkdir(parents=True)
    (user / ".cursor" / "plugins").symlink_to(user / "gone")


def _cursor_a_file(user: Path) -> None:
    user.mkdir(parents=True)
    (user / ".cursor").write_text("not a folder\n", encoding="utf-8")


@pytest.mark.parametrize("layout", [_readonly_cursor, _dangling_plugins, _cursor_a_file])
def test_cursor_s_plugin_folder_counts_only_when_a_run_makes_and_hides_it(proj, layout):
    from metabot.engines.cursor import CursorEngine

    user = sandbox.user_home()
    layout(user)
    try:
        if os.access(user / ".cursor", os.W_OK) and layout is _readonly_cursor:
            pytest.skip("this user writes any folder")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
            said = sandbox.foresee(CursorEngine(), proj, enabled=True)
            plan = sandbox.plan_for(CursorEngine(), proj, enabled=True)
    finally:
        if (user / ".cursor").is_dir() and not (user / ".cursor").is_symlink():
            (user / ".cursor").chmod(0o755)
    assert said == f"hides {len(plan.hidden)} here, with bwrap"


def test_cursor_run_inside_its_own_folder_keeps_the_plugin_folder_as_the_project_s(
    tmp_path, monkeypatch
):
    from metabot.engines.cursor import CursorEngine

    monkeypatch.setattr(sandbox, "find_bwrap", lambda: "/usr/bin/bwrap")
    inside = sandbox.user_home() / ".cursor"
    inside.mkdir(parents=True)
    said = sandbox.foresee(CursorEngine(), inside, enabled=True)
    plan = sandbox.plan_for(CursorEngine(), inside, enabled=True)
    count = len(plan.hidden)
    assert said == (f"hides {count} here, with bwrap" if count else "nothing to hide here")
