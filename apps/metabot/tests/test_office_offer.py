"""What an office run offers the person to pick: the branches that hold its work.

A relay whose desks changed no file still said "pick review",
and after copies whose work no later desk took up, it named a branch with
nothing on it while the work sat on a copy's branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metabot import desks, office_run
from metabot.cli import main
from metabot.desks import DeskGitError, is_ancestor
from metabot.gitcmd import GitError
from test_office_fan import FailsOn
from test_office_run import DESKS, TASK, DeskEngine, _build, _git, _repo

STAGES = "  (stages the relay's work in your checkout)"
NOTHING = "nothing to pick: no desk changed a file"
LEFT = (
    "nothing to pick: no desk that finished left work on its branch; "
    "metabot desks list shows every desk branch"
)


def _write(name: str, only: str | None = None):
    """An act that writes ``name`` in its worktree, or only in the worktree called ``only``."""

    def act(job) -> str:
        if only is None or Path(job.cwd).name == only:
            (Path(job.cwd) / name).write_text(f"{name}\n", encoding="utf-8")
            return f"WROTE {name}"
        return "nothing to change"

    return act


def _say(text: str):
    return lambda job: text


def _quiet() -> dict:
    return {
        "claude": DeskEngine("claude", act=_say("PLAN: write feature.txt")),
        "codex": DeskEngine("codex", act=_say("nothing to build")),
        "cursor": DeskEngine("cursor", act=_say("REVIEW: fine")),
    }


def _run(capsys, monkeypatch, root: Path, engines: dict, *extra: str) -> tuple[int, str, str]:
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code = main(["office", "run", "--dir", str(root), "-q", *extra, TASK])
    out = capsys.readouterr().out
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    return code, out, office_id


def _offered(out: str) -> list[str]:
    starts = ("next:", "pick one:", "nothing to pick")
    return [line for line in out.splitlines() if line.startswith(starts)]


FANNED = {**DESKS, "build": {**DESKS["build"], "fan": 2}}


def test_a_relay_that_changed_no_file_offers_nothing(tmp_path, capsys, monkeypatch):
    code, out, _ = _run(capsys, monkeypatch, _repo(tmp_path), _quiet())
    assert code == 0
    assert _offered(out) == [NOTHING]


def test_a_relay_offers_its_last_desk_when_it_holds_the_work(tmp_path, capsys, monkeypatch):
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_build)}
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path), engines)
    assert code == 0
    assert _offered(out) == [f"next: metabot desks pick {office_id}/review{STAGES}"]


def test_a_copys_work_no_later_desk_took_up_is_offered(tmp_path, capsys, monkeypatch):
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_write("feature.txt", "build.1"))}
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 0
    assert _offered(out) == [f"next: metabot desks pick {office_id}/build.1{STAGES}"]


def test_a_copy_that_holds_the_earlier_desks_work_too_is_the_one_offered(
    tmp_path, capsys, monkeypatch
):
    engines = {
        "claude": DeskEngine("claude", act=_write("plan.txt")),
        "codex": DeskEngine("codex", act=_write("feature.txt", "build.1")),
        "cursor": DeskEngine("cursor", act=_say("REVIEW: fine")),
    }
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 0  # review holds only plan's work; build.1 holds plan's and its own
    assert _offered(out) == [f"next: metabot desks pick {office_id}/build.1{STAGES}"]


def test_a_later_desks_own_work_and_a_copys_are_both_offered(tmp_path, capsys, monkeypatch):
    engines = {
        **_quiet(),
        "codex": DeskEngine("codex", act=_write("feature.txt", "build.1")),
        "cursor": DeskEngine("cursor", act=_write("review.txt")),
    }
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 0
    assert _offered(out) == [
        f"pick one: metabot desks pick {office_id}/build.1",
        f"pick one: metabot desks pick {office_id}/review",
    ]


def test_copies_at_one_commit_are_each_offered(tmp_path, capsys, monkeypatch):
    desks = {**DESKS, "review": {**DESKS["review"], "fan": 2}}
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_build)}
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path, desks), engines)
    assert code == 0  # both copies hold build's work and changed nothing: either will do
    assert _offered(out) == [
        f"pick one: metabot desks pick {office_id}/review.1",
        f"pick one: metabot desks pick {office_id}/review.2",
    ]


def test_a_fan_offers_only_the_desks_that_changed_a_file(tmp_path, capsys, monkeypatch):
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_build)}
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path), engines, "--fan")
    assert code == 0
    assert _offered(out) == [f"pick one: metabot desks pick {office_id}/build"]


def test_a_fan_that_changed_no_file_offers_nothing(tmp_path, capsys, monkeypatch):
    code, out, _ = _run(capsys, monkeypatch, _repo(tmp_path), _quiet(), "--fan")
    assert code == 0
    assert _offered(out) == [NOTHING]


def test_when_only_a_failed_copy_changed_a_file_it_is_not_offered(tmp_path, capsys, monkeypatch):
    engines = {
        **_quiet(),
        "codex": FailsOn("codex", "build.1", act=_write("feature.txt", "build.1")),
    }
    code, out, office_id = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 1  # a copy failed, and the relay went on with the other
    assert _offered(out) == [LEFT]


def test_a_relay_that_stopped_at_a_failed_desk_offers_nothing(tmp_path, capsys, monkeypatch):
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_build, fail="codex exited 1")}
    code, out, _ = _run(capsys, monkeypatch, _repo(tmp_path), engines)
    assert code == 1
    assert _offered(out) == []


def _drop_branch_of(label: str):
    """An act that deletes another desk's branch of this office run, as a careless desk might."""

    def act(job) -> str:
        cwd = Path(job.cwd)
        office_id = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").split("/")[1]
        _git(cwd, "update-ref", "-d", f"refs/heads/metabot/{office_id}/{label}")
        return "REVIEW: fine"

    return act


def test_a_desk_branch_that_is_gone_is_not_offered(tmp_path, capsys, monkeypatch):
    engines = {
        **_quiet(),
        "codex": DeskEngine("codex", act=_write("feature.txt", "build.1")),
        "cursor": DeskEngine("cursor", act=_drop_branch_of("build.1")),
    }
    code, out, _ = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 0
    assert _offered(out) == [LEFT]  # build.1 changed a file, but there is no branch to pick


@pytest.mark.parametrize("error", [DeskGitError("not a commit here"), GitError((), None, "slow")])
def test_a_branch_git_cannot_read_is_not_offered(tmp_path, capsys, monkeypatch, error):
    real = office_run.resolve_commit

    def unreadable(repo, rev):
        if rev.startswith("refs/heads/") and rev.endswith("/build.1"):
            raise error
        return real(repo, rev)

    monkeypatch.setattr(office_run, "resolve_commit", unreadable)
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_write("feature.txt", "build.1"))}
    code, out, _ = _run(capsys, monkeypatch, _repo(tmp_path, FANNED), engines)
    assert code == 0
    assert _offered(out) == [LEFT]


def test_is_ancestor_reads_the_history(tmp_path):
    repo = _repo(tmp_path)
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    second = _git(repo, "rev-parse", "HEAD").strip()
    assert is_ancestor(repo, first, second)
    assert not is_ancestor(repo, second, first)
    assert not is_ancestor(repo, "f" * 40, second)  # no such commit


def test_is_ancestor_is_false_when_git_cannot_say(tmp_path, monkeypatch):
    def broken(*args, **kw):
        raise GitError(("merge-base",), None, "timed out after 30s")

    monkeypatch.setattr(desks, "git", broken)
    assert not is_ancestor(tmp_path, "a" * 40, "b" * 40)


def test_an_advisor_is_never_offered(tmp_path, capsys, monkeypatch):
    desks = {**DESKS, "qa": {"role": "roles/qa.md", "harness": "claude", "advises": "build"}}
    root = _repo(tmp_path, desks)
    # A branch named None: an advisor has no branch, and must not be read as this one.
    _git(root, "checkout", "-q", "-b", "None")
    (root / "side.txt").write_text("side\n", encoding="utf-8")
    _git(root, "add", "side.txt")
    _git(root, "commit", "-q", "-m", "side")
    _git(root, "checkout", "-q", "main")
    engines = {**_quiet(), "codex": DeskEngine("codex", act=_build)}
    code, out, office_id = _run(capsys, monkeypatch, root, engines)
    assert code == 0 and "advice of qa on build:" in out
    assert _offered(out) == [f"next: metabot desks pick {office_id}/review{STAGES}"]
