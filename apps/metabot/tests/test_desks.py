"""Desks work in git worktrees outside the project; pick stages, never commits."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import private_mode
from metabot.cli import main
from metabot.desks import (
    TRAILER,
    DeskGitError,
    commit_desk,
    create_worktree,
    desk_branch,
    desk_label,
    diff_stat,
    pick,
    relink,
    resolve_commit,
    restore_worktree,
)
from metabot.desks import clean as clean_run


def _git(repo: Path, *args: str, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=check
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "MetaBot Test")
    _git(r, "config", "user.email", "metabot-test@example.invalid")
    (r / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "first")
    return r


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _desk_work(repo: Path, run: str = "mb-1", label: str = "dev", text: str = "hi\n") -> Path:
    wt = create_worktree(repo, run, label, "HEAD")
    (wt / "greeting.txt").write_text(text, encoding="utf-8")
    assert commit_desk(wt, label.split(".")[0], "Add a greeting", f"{run}.{label}")
    return wt


def _snapshot(repo: Path) -> dict[str, bytes]:
    files = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    files[repo / ".git" / "index"] = (repo / ".git" / "index").read_bytes()
    return {str(k): v for k, v in files.items()}


def test_names(home):
    assert desk_label("dev") == "dev"
    assert desk_label("dev", 2) == "dev.2"
    assert desk_branch("mb-1", "dev.2") == "metabot/mb-1/dev.2"
    for bad_run, bad_label in (("../x", "dev"), ("mb-1", "a b"), ("mb-1", "-x"), ("mb-1", "")):
        with pytest.raises(ValueError):
            desk_branch(bad_run, bad_label)


def test_worktree_is_outside_the_project(repo, home):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    assert wt == home / "desks" / "mb-1" / "dev"
    assert (wt / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert _git(repo, "branch", "--list", "metabot/mb-1/dev").strip()
    assert _git(repo, "status", "--porcelain") == ""
    assert oct((home / "desks" / "mb-1").stat().st_mode & 0o777) == "0o700"


def test_the_desks_folder_is_private_whatever_the_umask(repo, home):
    old = os.umask(0o002)
    try:
        create_worktree(repo, "mb-1", "dev", "HEAD")
    finally:
        os.umask(old)
    # another user could list the run ids in a folder the umask made
    assert private_mode(home / "desks") == 0o700
    assert private_mode(home / "desks" / "mb-1") == 0o700


def test_a_second_worktree_for_the_same_desk_is_refused(repo):
    create_worktree(repo, "mb-1", "dev", "HEAD")
    with pytest.raises(DeskGitError):
        create_worktree(repo, "mb-1", "dev", "HEAD")


def test_a_base_that_is_not_a_commit_is_refused(repo):
    for base in ("nope", "--help", "HEAD~5"):
        with pytest.raises(DeskGitError):
            create_worktree(repo, "mb-1", "dev", base)


def test_commit_carries_the_run_id(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    (wt / "greeting.txt").write_text("hi\n", encoding="utf-8")
    (wt / ".metabot" / "inbox").mkdir(parents=True)
    (wt / ".metabot" / "inbox" / "plan.md").write_text("the plan\n", encoding="utf-8")
    sha = commit_desk(wt, "dev", "Add a greeting\nand more detail", "mb-1.dev")
    assert sha == _head(wt)
    message = _git(wt, "log", "-1", "--format=%B").strip().splitlines()
    assert message[0] == "dev: Add a greeting"
    assert message[-1] == f"{TRAILER}: mb-1.dev"
    assert _git(wt, "log", "-1", "--format=%an <%ae>").strip() == (
        "MetaBot Test <metabot-test@example.invalid>"
    )
    assert _git(wt, "show", "--name-only", "--format=").split() == ["greeting.txt"]


def test_nothing_changed_commits_nothing(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    assert commit_desk(wt, "dev", "task", "mb-1.dev") is None
    (wt / ".metabot").mkdir()
    (wt / ".metabot" / "note.md").write_text("x\n", encoding="utf-8")
    assert commit_desk(wt, "dev", "task", "mb-1.dev") is None
    assert _head(wt) == _head(repo)


def test_a_desk_that_left_its_branch_is_reported(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    _git(wt, "checkout", "-q", "-b", "elsewhere")
    (wt / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(DeskGitError, match="left its branch"):
        commit_desk(wt, "dev", "task", "mb-1.dev", branch="metabot/mb-1/dev")
    assert _git(wt, "log", "-1", "--format=%s").strip() == "first"


def _point_at_the_project(wt: Path, repo: Path) -> None:
    """What a search and replace over a desk's work can do to its .git file."""
    (wt / ".git").write_text(f"gitdir: {repo / '.git'}\n", encoding="utf-8")


def _stage_something(repo: Path) -> bytes:
    (repo / "staged.txt").write_text("the person's\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    return (repo / ".git" / "index").read_bytes()


def test_a_desk_that_pointed_its_git_file_at_the_project_commits_on_its_own_branch(repo):
    first = _head(repo)
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    link = (wt / ".git").read_bytes()
    index = _stage_something(repo)
    (wt / "greeting.txt").write_text("hi\n", encoding="utf-8")
    _point_at_the_project(wt, repo)
    sha = commit_desk(wt, "dev", "task", "mb-1.dev", branch=desk_branch("mb-1", "dev"))
    assert (wt / ".git").read_bytes() == link
    assert sha and _git(repo, "rev-parse", desk_branch("mb-1", "dev")).strip() == sha
    assert _head(repo) == first  # nothing landed on the person's branch
    assert (repo / ".git" / "index").read_bytes() == index  # their staged file is still staged


def test_restoring_a_desk_that_pointed_its_git_file_at_the_project_leaves_the_project(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    index = _stage_something(repo)
    status = _git(repo, "status", "--porcelain=v1")
    (wt / "scratch.txt").write_text("what ran after the desk\n", encoding="utf-8")
    _point_at_the_project(wt, repo)
    restore_worktree(wt)
    assert (repo / ".git" / "index").read_bytes() == index
    assert _git(repo, "status", "--porcelain=v1") == status
    assert not (wt / "scratch.txt").exists()


def test_relink_leaves_a_worktree_it_did_not_make_alone(repo, tmp_path):
    other = tmp_path / "other"
    _git(repo, "worktree", "add", "-q", "--detach", str(other))
    (other / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
    put_back = relink(other)
    assert put_back is False
    assert (other / ".git").read_text(encoding="utf-8") == "gitdir: nowhere\n"


def test_task_with_quotes_and_a_leading_dash_is_one_message(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    (wt / "x.txt").write_text("x\n", encoding="utf-8")
    task = '--amend "quoted" ; rm -rf / $(id)'
    commit_desk(wt, "dev", task, "mb-1.dev")
    assert _git(wt, "log", "-1", "--format=%s").strip() == f"dev: {task}"
    assert _git(wt, "rev-list", "--count", "HEAD").strip() == "2"


def test_control_characters_in_the_task_never_reach_the_log(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    (wt / "x.txt").write_text("x\n", encoding="utf-8")
    commit_desk(wt, "dev", "\n\n  a\x1b[2Jb\x07c\u202ed" + "z" * 200, "mb-1.dev")
    subject = _git(wt, "log", "-1", "--format=%s").strip()
    assert subject.startswith("dev: a [2Jb c dz")  # the escape byte goes; its text is inert
    assert len(subject) <= 72
    assert all(ch.isprintable() for ch in subject)


def test_hooks_never_run(repo, tmp_path):
    marker = tmp_path / "hook-ran"
    hooks = repo / ".git" / "hooks"
    for name in ("post-checkout", "pre-commit", "commit-msg", "post-commit", "post-merge"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
    _desk_work(repo)
    pick(repo, "metabot/mb-1/dev")
    assert not marker.exists()


def test_a_git_dir_in_the_environment_is_ignored(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    _desk_work(repo)
    assert "metabot/mb-1/dev" in _git(repo, "branch", "--list", "metabot/*")


def test_diff_stat(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    (wt / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    (wt / "new.txt").write_text("a\nb\nc\n", encoding="utf-8")
    commit_desk(wt, "dev", "two files", "mb-1.dev")
    assert diff_stat(repo, _head(repo), resolve_commit(repo, "metabot/mb-1/dev")) == (2, 4, 0)


def test_pick_stages_and_never_commits(repo):
    before = _head(repo)
    _desk_work(repo)
    assert pick(repo, "metabot/mb-1/dev") == ["greeting.txt"]
    assert _head(repo) == before
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["greeting.txt"]
    squash = (repo / ".git" / "SQUASH_MSG").read_text(encoding="utf-8")
    assert f"{TRAILER}: mb-1.dev" in squash


def test_pick_of_work_already_in_the_checkout_stages_nothing(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    assert commit_desk(wt, "dev", "task", "mb-1.dev") is None
    assert pick(repo, "metabot/mb-1/dev") == []


def _staged(repo: Path) -> None:
    (repo / "hello.txt").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")


def _unstaged(repo: Path) -> None:
    (repo / "hello.txt").write_text("changed\n", encoding="utf-8")


def _mid_merge(repo: Path) -> None:
    (repo / ".git" / "MERGE_HEAD").write_text(_head(repo) + "\n", encoding="utf-8")


def _mid_rebase(repo: Path) -> None:
    (repo / ".git" / "rebase-merge").mkdir()


@pytest.mark.parametrize("make_dirty", [_staged, _unstaged, _mid_merge, _mid_rebase])
def test_pick_refuses_a_checkout_that_is_not_clean(repo, make_dirty):
    _desk_work(repo)
    make_dirty(repo)
    before = _snapshot(repo)
    with pytest.raises(DeskGitError, match="cannot pick"):
        pick(repo, "metabot/mb-1/dev")
    assert _snapshot(repo) == before
    assert not (repo / "greeting.txt").exists()


def test_an_untracked_file_in_the_checkout_does_not_block_a_pick(repo):
    _desk_work(repo)
    (repo / "notes.txt").write_text("mine\n", encoding="utf-8")
    assert pick(repo, "metabot/mb-1/dev") == ["greeting.txt"]


def test_pick_only_takes_metabot_branches(repo):
    _git(repo, "branch", "feature")
    with pytest.raises(DeskGitError, match="metabot/"):
        pick(repo, "feature")
    with pytest.raises(DeskGitError):
        pick(repo, "metabot/mb-1/nope")


def test_a_conflict_is_left_for_the_person_with_the_undo(repo):
    wt = create_worktree(repo, "mb-1", "dev", "HEAD")
    (wt / "hello.txt").write_text("desk\n", encoding="utf-8")
    commit_desk(wt, "dev", "task", "mb-1.dev")
    (repo / "hello.txt").write_text("person\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "mine")
    with pytest.raises(DeskGitError) as caught:
        pick(repo, "metabot/mb-1/dev")
    assert "hello.txt" in str(caught.value)
    assert "git reset --merge" in str(caught.value)
    _git(repo, "reset", "-q", "--merge")
    assert _git(repo, "status", "--porcelain") == ""


def test_clean_asks_with_the_list_and_removes_only_that_runs_desks(repo, home):
    _desk_work(repo, "mb-1", "dev")
    _desk_work(repo, "mb-1", "review")
    (home / "desks" / "mb-1" / "dev" / ".metabot").mkdir()
    (home / "desks" / "mb-1" / "dev" / ".metabot" / "inbox.md").write_text("x", encoding="utf-8")
    keep = _desk_work(repo, "mb-2", "dev")
    _git(repo, "branch", "metabot-notes")
    asked: list[list[str]] = []

    def no(items: list[str]) -> bool:
        asked.append(items)
        return False

    assert clean_run(repo, "mb-1", no).removed == []
    assert sorted(asked[0]) == sorted(
        [
            str(home / "desks" / "mb-1" / "dev"),
            str(home / "desks" / "mb-1" / "review"),
            "metabot/mb-1/dev",
            "metabot/mb-1/review",
        ]
    )
    assert (home / "desks" / "mb-1" / "dev").exists()

    result = clean_run(repo, "mb-1", lambda items: True)
    assert len(result.removed) == 4 and result.kept == {}
    assert not (home / "desks" / "mb-1").exists()
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert sorted(branches) == ["main", "metabot-notes", "metabot/mb-2/dev"]
    assert keep.exists()


def test_clean_keeps_a_desk_with_work_not_committed(repo, home):
    _desk_work(repo, "mb-1", "dev")
    wt = _desk_work(repo, "mb-1", "review")
    (wt / "hello.txt").write_text("not committed\n", encoding="utf-8")
    result = clean_run(repo, "mb-1", lambda items: True)
    assert str(wt) in result.kept and "metabot/mb-1/review" in result.kept
    assert sorted(result.removed) == sorted(
        [str(home / "desks" / "mb-1" / "dev"), "metabot/mb-1/dev"]
    )
    assert (wt / "hello.txt").read_text(encoding="utf-8") == "not committed\n"


def test_clean_never_touches_a_worktree_outside_metabots_home(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _git(repo, "worktree", "add", "-q", "-b", "metabot/mb-1/dev", str(elsewhere))
    (elsewhere / ".metabot").mkdir()
    result = clean_run(repo, "mb-1", lambda items: True)
    assert result.removed == []
    assert (elsewhere / ".metabot").exists()
    assert "metabot/mb-1/dev" in result.kept


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_desks_list_pick_and_clean_through_the_cli(repo, capsys, monkeypatch):
    _desk_work(repo)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, out, err = _cli(capsys, "desks", "list", "--dir", str(repo))
    assert code == 0, err
    assert "mb-1/dev" in out and "1 file" in out and "dev: Add a greeting" in out
    code, out, err = _cli(capsys, "desks", "pick", "mb-1/dev", "--dir", str(repo))
    assert code == 0, err
    assert "greeting.txt" in out and "nothing was committed" in out
    _git(repo, "reset", "-q", "--merge")
    code, out, err = _cli(capsys, "desks", "clean", "mb-1", "--dir", str(repo))
    assert code == 2 and "--yes" in err
    code, out, err = _cli(capsys, "desks", "clean", "mb-1", "--dir", str(repo), "--yes")
    assert code == 0, err
    assert "metabot/mb-1/dev" in out
    assert _git(repo, "branch", "--list", "metabot/*") == ""


def test_desks_outside_a_repository(tmp_path, capsys):
    code, _, err = _cli(capsys, "desks", "list", "--dir", str(tmp_path))
    assert code == 2 and "git" in err
