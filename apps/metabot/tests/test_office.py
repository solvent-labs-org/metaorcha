"""MetaBot reads Code Desks' own office.yaml, with three keys of its own."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES, not_on_path
from metabot import office as office_module
from metabot.cli import main
from metabot.engines import missing_command
from metabot.office import (
    FAN_CAP_MAX,
    MAX_FILE_BYTES,
    TURN_CAP_DEFAULT,
    YOU,
    OfficeError,
    desk_engine,
    desk_problem,
    find_office,
    load_office,
)
from metabot.office_run import chosen_desks

GATES = {"ready": {"owner": ["verdict"], "engineer": ["tests", "evidence"]}}


@pytest.fixture(autouse=True)
def _no_pinned_office(monkeypatch):
    monkeypatch.delenv("DESK_OFFICE", raising=False)


def _office(root: Path, desks: dict, gates: dict | None = None, roles: bool = True, **top) -> Path:
    """An office folder: office.yaml, plus a role file for every in-folder role."""
    root.mkdir(parents=True, exist_ok=True)
    data = {"name": "t", **top, "gates": gates or GATES, "desks": desks}
    (root / "office.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    for spec in desks.values() if roles else ():
        role = spec.get("role") if isinstance(spec, dict) else None
        if isinstance(role, str) and role and not role.startswith(("/", "..")):
            path = root / role
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"You are {role}.\n", encoding="utf-8")
    return root


def _desk(**kw) -> dict:
    return {"role": "roles/x.md", **kw}


def test_reads_code_desks_own_template(tmp_path):
    root = tmp_path / "office"
    shutil.copytree(FIXTURES / "office", root)
    office = load_office(root)
    assert office.name == "my-office"
    assert list(office.desks) == ["pm", "dev", "review"]
    assert list(office.gates) == ["scope", "ready", "shipped"]
    assert all(gate.checks == {} for gate in office.gates.values())
    pm = office.desks["pm"]
    assert (pm.harness, pm.engine, pm.model, pm.budget) == ("claude", "claude", "opus", 10_000_000)
    assert pm.role_file == root.resolve() / ".office/roles/pm.md"
    assert desk_engine(office.desks["review"]) == ("codex", None)
    assert all(desk_problem(d) is None for d in office.desks.values())
    assert all(d.fan == 1 and d.turns == TURN_CAP_DEFAULT for d in office.desks.values())


def test_unknown_keys_are_ignored(tmp_path):
    desk = _desk(
        tier="xhigh",
        sessions=["dev-*"],
        kit=["bmad-dev-story"],
        overrides={"bmad-party-mode": {"x": 1}},
        colour="teal",
        branch="desk/dev",
        worktree=True,
    )
    office = load_office(_office(tmp_path, {"dev": desk}, cadence={"sprint_days": 7}, plans={}))
    assert office.desks["dev"].kit == ("bmad-dev-story",)
    assert desk_problem(office.desks["dev"]) is None


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor"])
def test_harness_maps_to_its_engine(tmp_path, harness):
    office = load_office(_office(tmp_path, {"d": _desk(harness=harness, model="m")}))
    assert desk_engine(office.desks["d"]) == (harness, "m")


def test_a_custom_desk_names_its_engine(tmp_path):
    desk = _desk(harness="custom", command="aider", engine="openai", model="qwen2.5:3b")
    office = load_office(_office(tmp_path, {"local": desk}))
    assert desk_engine(office.desks["local"]) == ("openai", "qwen2.5:3b")


@pytest.mark.parametrize(
    "command",
    [
        "metabot chat -e openai -m qwen2.5:3b",
        "metabot chat --engine openai --model qwen2.5:3b",
        "/usr/local/bin/metabot chat --engine=openai --model=qwen2.5:3b",
        "metabot ask -eopenai -mqwen2.5:3b",
    ],
)
def test_a_custom_desk_running_metabot_needs_no_engine_key(tmp_path, command):
    office = load_office(_office(tmp_path, {"local": _desk(harness="custom", command=command)}))
    assert desk_engine(office.desks["local"]) == ("openai", "qwen2.5:3b")


@pytest.mark.parametrize(
    ("command", "says"),
    [
        ("aider --model x", "engine:"),
        ("metabot chat", "-e"),
        ("metabot chat -e nope", "nope"),
        ("metabot 'chat", "engine:"),
    ],
)
def test_a_custom_desk_it_cannot_place_does_not_stop_the_office(tmp_path, command, says):
    desks = {"dev": _desk(), "other": _desk(harness="custom", command=command)}
    office = load_office(_office(tmp_path, desks))
    assert says in desk_problem(office.desks["other"])
    assert desk_problem(office.desks["dev"]) is None
    with pytest.raises(OfficeError):
        desk_engine(office.desks["other"])


@pytest.mark.parametrize(
    "desk",
    [
        _desk(model="--resume"),
        _desk(model="a b"),
        _desk(harness="custom", command="metabot chat -e openai -m --resume"),
        _desk(harness="custom", command="metabot chat -e openai --model=-x"),
    ],
)
def test_a_model_that_is_not_a_model_name_is_a_problem(tmp_path, desk):
    office = load_office(_office(tmp_path, {"dev": _desk(model="m-1.5:b"), "bad": desk}))
    assert "model" in desk_problem(office.desks["bad"])  # it would reach the engine's argv
    assert desk_problem(office.desks["dev"]) is None
    with pytest.raises(OfficeError, match="not a model name"):
        chosen_desks(office, ["bad"])  # refused before anything runs
    assert chosen_desks(office, ["dev"]) == ["dev"]


def test_an_any_desk_runs_on_the_roster(tmp_path):
    desks = {"pool": {"harness": "any"}, "pinned": {"harness": "any", "engine": "claude"}}
    office = load_office(_office(tmp_path, desks))
    assert "roster" in desk_problem(office.desks["pool"])
    roster = {"build": {"engine": "codex", "model": "m"}}
    assert desk_problem(office.desks["pool"], roster) is None
    assert desk_engine(office.desks["pool"], roster) == ("codex", "m")
    assert desk_engine(office.desks["pinned"]) == ("claude", None)


def test_a_desk_whose_engine_command_is_not_on_path_cannot_run(tmp_path, monkeypatch):
    not_on_path(monkeypatch, "codex", "cursor-agent")
    desks = {
        "dev": _desk(),
        "review": _desk(harness="codex"),
        "wrapped": _desk(harness="custom", command="metabot ask -e codex"),
        "local": _desk(harness="custom", command="metabot chat -e openai"),
        "pool": _desk(harness="any"),
    }
    office = load_office(_office(tmp_path, desks))
    assert desk_problem(office.desks["dev"]) is None
    assert desk_problem(office.desks["review"]) == "codex is not on PATH"
    assert desk_problem(office.desks["wrapped"]) == "codex is not on PATH"
    assert desk_problem(office.desks["local"]) is None
    roster = {"build": {"engine": "cursor"}}  # an any desk is checked on the engine it resolves to
    assert desk_problem(office.desks["pool"], roster) == "cursor-agent is not on PATH"
    assert desk_problem(office.desks["pool"], {"build": {"engine": "claude"}}) is None
    with pytest.raises(OfficeError, match="review: codex is not on PATH"):
        chosen_desks(office, ["dev", "review"])  # refused before anything runs
    assert chosen_desks(office, ["dev", "local"]) == ["dev", "local"]


def test_missing_command_reads_path_and_an_engine_with_no_command_is_never_missing(
    tmp_path, monkeypatch
):
    found = tmp_path / "bin"
    found.mkdir()
    monkeypatch.setenv("PATH", str(found))
    names = ("claude", "codex", "cursor", "openai")
    assert [missing_command(e) for e in names] == ["claude", "codex", "cursor-agent", None]
    for binary in ("claude", "codex", "cursor-agent"):
        (found / binary).write_text("#!/bin/sh\n", encoding="utf-8")
        (found / binary).chmod(0o755)
    assert [missing_command(e) for e in names] == [None, None, None, None]


def test_desk_problem_reads_this_machines_path(tmp_path, monkeypatch):
    monkeypatch.setattr(office_module, "missing_command", missing_command)  # the real one
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    desks = {"review": _desk(harness="codex"), "local": _desk(harness="any", engine="openai")}
    office = load_office(_office(tmp_path, desks))
    assert desk_problem(office.desks["review"]) == "codex is not on PATH"
    assert desk_problem(office.desks["local"]) is None  # a model server runs no command


@pytest.mark.parametrize(
    ("desk", "where"),
    [
        (_desk(engine="nope"), "desks.d.engine"),
        (_desk(harness="claude", engine="openai"), "desks.d.engine"),
        (_desk(fan=FAN_CAP_MAX + 1), "desks.d.fan"),
        (_desk(fan=0), "desks.d.fan"),
        (_desk(fan="two"), "desks.d.fan"),
        (_desk(fan=True), "desks.d.fan"),
        (_desk(turns=0), "desks.d.turns"),
        (_desk(turns=10_000), "desks.d.turns"),
    ],
)
def test_wrong_values_in_metabots_own_keys_are_errors(tmp_path, desk, where):
    with pytest.raises(OfficeError) as caught:
        load_office(_office(tmp_path, {"d": desk}))
    assert caught.value.where == where


def test_fan_over_this_machines_cap_cannot_run(tmp_path):
    root = _office(tmp_path, {"d": _desk(fan=4)})
    assert "cap of 3" in desk_problem(load_office(root).desks["d"])
    assert desk_problem(load_office(root, fan_cap=5).desks["d"]) is None
    assert desk_problem(load_office(root, fan_cap=99).desks["d"]) is None  # clamped to 5


@pytest.mark.parametrize("name", ["a b", "-x", "x;rm", "x.y", "a" * 33, "../x", "\u00e9"])
def test_a_name_that_cannot_be_a_branch_cannot_run(tmp_path, name):
    office = load_office(_office(tmp_path, {name: _desk(), "ok": _desk()}))
    assert "name" in desk_problem(office.desks[name])
    assert desk_problem(office.desks["ok"]) is None


def test_a_role_outside_the_office_folder_is_never_read(tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("private\n", encoding="utf-8")
    root = tmp_path / "office"
    root.mkdir()
    (root / "roles").mkdir()
    (root / "roles" / "link.md").symlink_to(outside)
    desks = {
        "abs": {"role": str(outside)},
        "up": {"role": "../secret.md"},
        "link": {"role": "roles/link.md"},
        "gone": {"role": "roles/missing.md"},
        "dir": {"role": "roles"},
    }
    office = load_office(_office(root, desks, roles=False))
    for name in ("abs", "up", "link"):
        assert "inside the office" in desk_problem(office.desks[name]), name
        assert office.desks[name].role_file is None
    assert "missing" in desk_problem(office.desks["gone"])
    assert "not a file" in desk_problem(office.desks["dir"])


def test_a_role_anywhere_in_the_project_is_fine(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "dev.md").write_text("You build.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    office = load_office(_office(repo / "ops", {"dev": {"role": "../docs/dev.md"}}))
    assert desk_problem(office.desks["dev"]) is None
    assert office.desks["dev"].role_file == (repo / "docs" / "dev.md").resolve()


@pytest.mark.parametrize("cwd", ["/", "..", "a/../../b"])
def test_a_cwd_outside_the_worktree_cannot_run(tmp_path, cwd):
    office = load_office(_office(tmp_path, {"d": _desk(cwd=cwd)}))
    assert "cwd" in desk_problem(office.desks["d"])


def test_advises(tmp_path):
    desks = {
        "dev": _desk(),
        "a": _desk(advises=True),
        "b": _desk(advises=["dev"]),
        "c": _desk(advises=[]),
    }
    office = load_office(_office(tmp_path, desks))
    assert office.desks["dev"].advises == ()
    assert office.desks["a"].advises == (YOU,)
    assert office.desks["b"].advises == ("dev",)
    assert office.desks["c"].advises == (YOU,)


def test_checks_are_read_per_gate_in_order(tmp_path):
    gates = {
        "ready": {
            "owner": ["verdict"],
            "engineer": ["tests", "lint"],
            "checks": {"tests": "  make test ", "lint": "ruff check ."},
            "observed": {"verdict": "true"},
        },
        "shipped": {"owner": ["acceptance"], "engineer": ["read-back"]},
    }
    office = load_office(_office(tmp_path, {"dev": _desk()}, gates=gates))
    assert list(office.gates) == ["ready", "shipped"]
    assert office.gates["ready"].checks == {"tests": "make test", "lint": "ruff check ."}
    assert office.gates["shipped"].checks == {}


def _write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "office.yaml").write_text(text, encoding="utf-8")
    return root


def _doc(desk: str = "role: r.md", gates: str = "{g: {owner: [x]}}", name: str = "t") -> str:
    head = f"name: {name}\n" if name else ""
    return f"{head}gates: {gates}\ndesks: {{d: {{{desk}}}}}\n"


BAD = {
    "a list": ("- 1\n", "office.yaml"),
    "no name": (_doc(name=""), "name"),
    "no gates": (_doc(gates="{}"), "gates"),
    "empty gate": (_doc(gates="{g: {}}"), "gates.g"),
    "no desks": ("name: t\ngates: {g: {owner: [x]}}\n", "desks"),
    "bad harness": (_doc("role: r.md, harness: vim"), "desks.d.harness"),
    "no role": (_doc("harness: claude"), "desks.d.role"),
    "custom, no command": (_doc("role: r.md, harness: custom"), "desks.d.command"),
    "check for no item": (_doc(gates="{g: {owner: [x], checks: {y: make}}}"), "gates.g.checks.y"),
    "empty check": (_doc(gates="{g: {owner: [x], checks: {x: ' '}}}"), "gates.g.checks.x"),
    "advises nobody": (_doc("role: r.md, advises: [ghost]"), "desks.d.advises"),
    "advises itself": (_doc("role: r.md, advises: [d]"), "desks.d.advises"),
    "advises false": (_doc("role: r.md, advises: false"), "desks.d.advises"),
    "kit not strings": (_doc("role: r.md, kit: [[a]]"), "desks.d.kit"),
    "budget words": (_doc("role: r.md, budget: lots"), "desks.d.budget"),
    "same name twice": (
        "name: t\ngates: {g: {owner: [x]}}\ndesks: {1: {role: r.md}, '1': {role: r.md}}\n",
        "desks.1",
    ),
    "yaml syntax": ("name: [t\n", "office.yaml"),
    "impossible date": ("name: t\ncadence: {start: 2026-13-45}\n", "office.yaml"),
    "deep nesting": ("name: " + "[" * 5000 + "]" * 5000 + "\n", "office.yaml"),
}


@pytest.mark.parametrize("case", list(BAD))
def test_a_malformed_office_is_one_clear_error(tmp_path, case):
    text, where = BAD[case]
    with pytest.raises(OfficeError) as caught:
        load_office(_write(tmp_path, text))
    assert caught.value.where == where


def test_an_oversized_or_unreadable_file_is_an_error(tmp_path):
    big = _write(tmp_path / "big", "# " + "x" * MAX_FILE_BYTES + "\n")
    with pytest.raises(OfficeError, match="larger"):
        load_office(big)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "office.yaml").write_bytes(b"name: \xff\xfe\n")
    with pytest.raises(OfficeError, match="UTF-8"):
        load_office(binary)
    with pytest.raises(OfficeError, match="cannot read"):
        load_office(tmp_path / "nowhere")


def test_find_office_walks_up(tmp_path, monkeypatch):
    root = _office(tmp_path / "proj", {"dev": _desk()})
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    assert find_office(deep) == root.resolve()
    assert find_office(tmp_path) is None
    monkeypatch.setenv("DESK_OFFICE", str(root))
    assert find_office(tmp_path) == root.resolve()


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_find_office_from_a_linked_worktree_answers_the_main_tree(tmp_path):
    repo = _office(tmp_path / "repo", {"dev": _desk()})
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "MetaBot Test")
    _git(repo, "config", "user.email", "metabot-test@example.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "office")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    (linked / "sub").mkdir()
    assert find_office(linked / "sub") == repo.resolve()


def _linked_office(tmp_path: Path, linked: Path | None = None) -> tuple[Path, Path]:
    """repo/team holds main-office, and a linked worktree's team folder worktree-office;
    the worktree is ``linked``, beside the repository unless given."""
    team = _office(tmp_path / "repo" / "team", {"dev": _desk()}, name="main-office")
    repo = team.parent
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "MetaBot Test")
    _git(repo, "config", "user.email", "metabot-test@example.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "office")
    linked = linked or tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    _office(linked / "team", {"dev": _desk()}, name="worktree-office")
    return team, linked / "team"


def test_a_main_tree_whose_office_cannot_be_read_is_named_not_swapped_for_the_worktree_s(
    tmp_path, capsys, path_checks
):
    team, here = _linked_office(tmp_path)
    team.chmod(0o600)
    try:
        if os.access(team, os.X_OK):
            pytest.skip("this user enters any folder")
        code, out, err = _check(capsys, "--dir", str(here))
    finally:
        team.chmod(0o755)
    assert code == 2 and "worktree-office" not in out
    assert f"cannot read {team.resolve() / 'office.yaml'}: Permission denied" in err


@pytest.mark.parametrize("shut", ["tree", "git", "record"])
def test_a_linked_worktree_git_cannot_read_still_names_the_main_tree_s_office(
    tmp_path, capsys, monkeypatch, path_checks, shut
):
    team, here = _linked_office(tmp_path)
    repo = team.parent
    door, mode = {
        "tree": (repo, 0o600),
        "git": (repo / ".git", 0o000),
        "record": (repo / ".git" / "worktrees" / "linked", 0o000),  # before: its own office
    }[shut]
    door.chmod(mode)
    try:
        if os.access(door, os.X_OK):
            pytest.skip("this user enters any folder")
        given = _check(capsys, "--dir", str(here))
        monkeypatch.chdir(here)
        found = _check(capsys)
    finally:
        door.chmod(0o755)
    for code, out, err in (given, found):
        assert "worktree-office" not in out
        if shut == "tree":  # git cannot enter the main tree, and neither can MetaBot
            assert code == 2
            assert f"cannot read {team.resolve() / 'office.yaml'}: Permission denied" in err
        else:  # git cannot read the repository, and the main tree's office is there to read
            assert code == 0 and "main-office" in out


def test_a_main_tree_whose_name_is_not_utf_8_is_named_as_well(tmp_path, capsys, path_checks):
    team, here = _linked_office(tmp_path / os.fsdecode(b"caf\xe9"))  # a .git file holds bytes
    repo = team.parent
    repo.chmod(0o600)
    try:
        if os.access(repo, os.X_OK):
            pytest.skip("this user enters any folder")
        code, out, err = _check(capsys, "--dir", str(here))
    finally:
        repo.chmod(0o755)
    assert "worktree-office" not in out  # before: taken for a pruned worktree, by another name
    assert code == 2 and "office.yaml: Permission denied" in err


@pytest.mark.parametrize("how", ["pruned", "replaced", "declined", "garbled"])
def test_a_worktree_git_no_longer_knows_or_declines_reads_its_own_office(
    tmp_path, capsys, monkeypatch, how
):
    team, here = _linked_office(tmp_path)
    repo = team.parent
    if how == "pruned":  # as git worktree prune leaves it
        shutil.rmtree(repo / ".git" / "worktrees" / "linked")
    elif how == "garbled":  # its .git file names a record no file can be: before, a traceback
        (here.parent / ".git").write_bytes(b"gitdir: /nowhere\x00x/.git/worktrees/linked\n")
    elif how == "replaced":  # the main tree moved, and another project stands where it was
        repo.rename(tmp_path / "moved")
        other = _office(tmp_path / "repo" / "team", {"dev": _desk()}, name="another-project")
        _git(other.parent, "init", "-q")
    else:  # git looks no higher than the worktree's folder, on purpose
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(here.parent))
    code, out, err = _check(capsys, "--dir", str(here))
    assert code == 0 and "worktree-office" in out, err


@pytest.mark.parametrize(
    "lost", [errno.ENOTCONN, errno.EIO, errno.ESTALE], ids=["ENOTCONN", "EIO", "ESTALE"]
)
def test_a_main_tree_on_a_disk_that_went_away_is_named_not_swapped_for_the_worktree_s(
    tmp_path, capsys, monkeypatch, lost
):
    team, here = _linked_office(tmp_path)
    repo = team.parent
    gone, stat = str(repo.resolve()), os.stat

    def stat_on_a_lost_mount(path, *args, **kwargs):  # as a network mount that dropped
        if os.fspath(path).startswith(gone):
            raise OSError(lost, os.strerror(lost), os.fspath(path))
        return stat(path, *args, **kwargs)

    repo.chmod(0o600)  # and git, which runs apart, cannot enter it either
    try:
        if os.access(repo, os.X_OK):
            pytest.skip("this user enters any folder")
        monkeypatch.setattr(os, "stat", stat_on_a_lost_mount)
        code, out, err = _check(capsys, "--dir", str(here))
        monkeypatch.undo()
    finally:
        repo.chmod(0o755)
    assert code == 2 and "worktree-office" not in out  # before: the worktree's own was read
    assert f"cannot read {team.resolve() / 'office.yaml'}" in err


def test_office_check_from_a_linked_worktree_reads_the_office_its_dir_reads(
    tmp_path, capsys, monkeypatch, path_checks
):
    _team, here = _linked_office(tmp_path)
    shut = tmp_path / "shut"
    shut.mkdir()
    (shut / "office.yaml").write_text("name: shut\n", encoding="utf-8")
    (here / "office.yaml").unlink()
    (here / "office.yaml").symlink_to(shut / "office.yaml")
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        given = _check(capsys, "--dir", str(here))
        monkeypatch.chdir(here)
        found = _check(capsys)
    finally:
        shut.chmod(0o755)
    assert found == given
    assert found[0] == 0 and "main-office" in found[1]


def _removed_folder(tmp_path: Path, monkeypatch) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()


def test_office_and_desks_commands_from_a_removed_folder_say_why(tmp_path, capsys, monkeypatch):
    _removed_folder(tmp_path, monkeypatch)
    for argv in (
        ["office", "check"],
        ["office", "trust", "--yes"],
        ["office", "run", "hi"],
        ["desks", "list"],
        ["desks", "pick", "r/dev"],
        ["desks", "clean", "r", "--yes"],
    ):
        assert main(argv) == 2, argv
        out, err = capsys.readouterr()
        assert "the current folder cannot be read" in err and "give --dir" in err, argv


def test_a_relative_pinned_office_from_a_removed_folder_says_why(tmp_path, capsys, monkeypatch):
    _office(tmp_path / "off", {"dev": _desk()})
    _removed_folder(tmp_path, monkeypatch)
    monkeypatch.setenv("DESK_OFFICE", "../off")
    code, out, err = _check(capsys)
    assert code == 2 and out == ""
    assert "cannot read ../off/office.yaml: the current folder cannot be read" in err


def _check(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["office", "check", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_office_check_prints_desks_engines_and_checks(tmp_path, capsys):
    gates = {
        "ready": {"owner": ["verdict"], "engineer": ["tests"], "checks": {"tests": "make test"}}
    }
    desks = {
        "dev": _desk(model="opus", budget="36_000_000", kit=["bmad-dev-story"]),
        "review": _desk(harness="codex", advises=["dev"]),
        "local": _desk(harness="custom", command="metabot chat -e openai -m qwen2.5:3b", fan=2),
        "aider": _desk(harness="custom", command="aider"),
    }
    root = _office(tmp_path, desks, gates=gates)
    code, out, err = _check(capsys, "--dir", str(root))
    assert code == 0, err
    lines = out.splitlines()
    dev = next(line for line in lines if line.strip().startswith("dev "))
    assert "claude/opus" in dev and "36000000" in dev and "bmad-dev-story" in dev
    review = next(line for line in lines if line.strip().startswith("review "))
    assert "codex" in review and "advises dev" in review
    local = next(line for line in lines if line.strip().startswith("local "))
    assert "openai/qwen2.5:3b" in local and "fan 2" in local
    assert any("ready:tests" in line and "make test" in line for line in lines)
    assert any(line.strip().startswith("! aider:") and "engine:" in line for line in lines)


def test_office_check_names_a_folder_it_cannot_reach_or_enter(tmp_path, capsys):
    import os

    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    code, _out, err = _check(capsys, "--dir", str(loop))
    assert code == 2 and err == f"metabot: {loop} is not a directory\n"
    shut = _office(tmp_path / "shut", {"dev": _desk()})
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        code, _out, err = _check(capsys, "--dir", str(shut))
    finally:
        shut.chmod(0o755)
    assert code == 2 and "office.yaml: cannot read" in err and "Permission denied" in err


def test_office_check_from_a_folder_it_cannot_look_in_says_why(
    tmp_path, capsys, monkeypatch, path_checks
):
    shut = tmp_path / "shut"
    here = shut / "here"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        code, out, err = _check(capsys)
    finally:
        shut.chmod(0o755)
    assert code == 2 and out == ""
    assert f"cannot read {here / 'office.yaml'}: Permission denied" in err


def test_office_check_names_a_pinned_office_that_is_a_loop_of_links(tmp_path, capsys, monkeypatch):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("DESK_OFFICE", str(loop))
    assert find_office(tmp_path) == loop
    code, out, err = _check(capsys)
    assert code == 2 and out == ""
    assert f"cannot read {loop / 'office.yaml'}: Too many levels of symbolic links" in err


def test_office_check_names_a_desk_whose_engine_command_is_not_on_path(
    tmp_path, capsys, monkeypatch
):
    not_on_path(monkeypatch, "codex")
    root = _office(tmp_path, {"dev": _desk(), "review": _desk(harness="codex")})
    code, out, err = _check(capsys, "--dir", str(root))
    assert code == 0, err
    lines = [line.strip() for line in out.splitlines()]
    review = next(line for line in lines if line.startswith("review "))
    assert "codex" in review  # what it would run on is still shown
    assert "cannot run here" in lines
    assert "! review: codex is not on PATH" in lines
    assert not any(line.startswith("! dev:") for line in lines)


def test_office_check_finds_the_office_from_a_subfolder(tmp_path, capsys, monkeypatch):
    root = _office(tmp_path, {"dev": _desk()})
    (root / "src").mkdir()
    monkeypatch.chdir(root / "src")
    code, out, _ = _check(capsys)
    assert code == 0 and "dev" in out


def test_office_check_without_an_office(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = _check(capsys)
    assert code == 2 and out == ""
    assert "no office.yaml" in err


def test_office_check_reports_a_malformed_file(tmp_path, capsys):
    code, _, err = _check(capsys, "--dir", str(_write(tmp_path, "name: t\n")))
    assert code == 2 and "gates" in err


def test_office_check_cannot_repaint_the_terminal(tmp_path, capsys):
    evil = "x\x1b]0;owned\x07\x1b[2J"
    desks = {evil: _desk(model=evil)}
    code, out, err = _check(capsys, "--dir", str(_office(tmp_path, desks, name=evil)))
    assert code == 0, err
    assert "\x1b" not in out and "\x07" not in out


def _office_text(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "r.md").write_text("role\n", encoding="utf-8")
    (root / "office.yaml").write_text(text, encoding="utf-8")
    return root


BASE = """\
name: t
gates: {g: {owner: [x]}}
desks:
  d:
    role: r.md
    harness: claude
"""


def test_office_check_names_a_cwd_symlink_outside_the_worktree(tmp_path, capsys, home):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "office"
    root.mkdir()
    (root / "r.md").write_text("role\n", encoding="utf-8")
    (root / "out").symlink_to(outside)
    (root / "office.yaml").write_text(BASE + "    cwd: out\n", encoding="utf-8")
    code = main(["office", "check", "--dir", str(root)])
    err = capsys.readouterr().err
    assert code == 2 and "cwd" in err


def test_a_cwd_symlink_inside_the_worktree_can_run(tmp_path):
    root = tmp_path / "office"
    root.mkdir()
    (root / "r.md").write_text("role\n", encoding="utf-8")
    inside = root / "work"
    inside.mkdir()
    (root / "here").symlink_to(inside)
    (root / "office.yaml").write_text(BASE + "    cwd: here\n", encoding="utf-8")
    assert load_office(root).desks["d"].problem is None


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("- 1\n", "office.yaml"),
        ("name: t\ngates: {g: {owner: [x]}}\ndesks: [a]\n", "desks"),
        ("name: t\ngates: {g: {owner: [x]}}\ndesks: hello\n", "desks"),
        ("name: t\ngates: {g: {owner: [x]}}\ndesks:\n", "desks"),
        (
            "name: t\ngates: {g: {owner: [x]}}\ndesks: {d: {role: 3, harness: claude}}\n",
            "desks.d.role",
        ),
        (BASE + "    fan: -5\n", "desks.d.fan"),
        (BASE + "    fan: 999999999999\n", "desks.d.fan"),
        (BASE + "    turns: -1\n", "desks.d.turns"),
    ],
)
def test_a_hostile_office_is_a_named_error(tmp_path, text, where):
    with pytest.raises(OfficeError) as caught:
        load_office(_office_text(tmp_path / "office", text))
    assert caught.value.where == where


def test_a_negative_budget_cannot_run(tmp_path):
    desk = load_office(_office_text(tmp_path / "office", BASE + "    budget: -3\n")).desks["d"]
    assert desk.problem and "budget" in desk.problem


def test_an_alias_bomb_finishes_quickly(tmp_path):
    lines = ["a0: &a0 ['x']"]
    prev = "a0"
    for i in range(1, 8):
        name = f"a{i}"
        lines.append(f"{name}: &{name} [" + ",".join([f"*{prev}"] * 10) + "]")
        prev = name
    text = "name: t\n" + "\n".join(lines) + "\n"
    started = time.monotonic()
    with pytest.raises(OfficeError):
        load_office(_office_text(tmp_path / "office", text))
    assert time.monotonic() - started < 2


def test_a_python_tag_is_refused(tmp_path):
    text = "name: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(OfficeError) as caught:
        load_office(_office_text(tmp_path / "office", text))
    assert caught.value.where == "office.yaml"


def test_duplicate_keys_keep_the_later_value_and_still_check_it(tmp_path):
    text = BASE + "    role: r.md\n    role: ../outside.md\n"
    desk = load_office(_office_text(tmp_path / "office", text)).desks["d"]
    assert desk.role_file is None
    assert desk.problem and "missing" in desk.problem


def test_a_role_symlink_outside_the_project_is_not_read(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("nope\n", encoding="utf-8")
    root = tmp_path / "office"
    root.mkdir()
    (root / "job.md").symlink_to(outside)
    (root / "office.yaml").write_text(
        "name: t\ngates: {g: {owner: [x]}}\ndesks: {d: {role: job.md, harness: claude}}\n",
        encoding="utf-8",
    )
    desk = load_office(root).desks["d"]
    assert desk.role_file is None
    assert desk.problem and "role" in desk.problem
