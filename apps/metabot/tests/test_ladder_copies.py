"""The ladder's checks and copies: a failing check climbs, and a failed rung leaves nothing."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import private_mode
from fake_chat import answer, call
from metabot import commands, ladder_copies
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.desks import DeskGitError, run_items
from metabot.ladder import ANSWERED, CLIMBED, STOPPED, Rung, judge, load_ladder, run_ladder
from metabot.ladder_copies import Copies
from metabot.paths import metabot_home, receipt_path
from metabot.receipts import verify_file

RIGHT = "grep -qx right answer.txt"  # a check the classifier calls a read


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "MetaBot Test")
    _git(r, "config", "user.email", "metabot-test@example.invalid")
    (r / "hello.txt").write_text("hello\n")
    (r / "keep.txt").write_text("keep\n")
    (r / ".gitignore").write_text(".env\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "first")
    return r


def _state(repo: Path) -> dict:
    """Everything of the person's a ladder must not touch."""
    return {
        "status": _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "index": (repo / ".git" / "index").read_bytes(),
        "files": {
            str(p.relative_to(repo)): p.read_bytes()
            for p in sorted(repo.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        },
    }


def _dirty(repo: Path) -> None:
    (repo / "hello.txt").write_text("hello, changed\n")  # modified, not staged
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")  # staged, not committed
    (repo / "new.txt").write_text("untracked\n")
    (repo / "keep.txt").unlink()  # deleted, not staged
    (repo / ".env").write_text("SECRET=1\n")  # ignored


# ── the snapshot and the copies ─────────────────────────────────────────────


def test_a_copy_holds_the_project_as_it_stands_and_the_person_s_checkout_is_untouched(repo):
    _dirty(repo)
    before = _state(repo)
    copies = Copies.start(repo, "L")
    assert copies is not None
    wt = copies.make(1)
    assert (wt / "hello.txt").read_text() == "hello, changed\n"
    assert (wt / "staged.txt").read_text() == "staged\n"
    assert (wt / "new.txt").read_text() == "untracked\n"
    assert not (wt / "keep.txt").exists()
    assert not (wt / ".env").exists()  # an ignored file never goes into a copy
    assert wt.is_relative_to(metabot_home())  # outside the project
    assert _state(repo) == before  # index, tree, refs and files: byte for byte
    assert "metabot/" not in _git(repo, "branch", "--list", "--all")


def test_a_copy_works_in_the_person_s_subfolder(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("a\n")
    copies = Copies.start(repo / "sub", "L")
    assert copies is not None
    where = copies.make(1)
    assert where.name == "sub" and (where / "a.txt").read_text() == "a\n"


def test_outside_git_there_are_no_copies(tmp_path):
    assert Copies.start(tmp_path, "L") is None


def test_the_answer_s_work_comes_back_new_changed_deleted_and_binary(repo):
    copies = Copies.start(repo, "L")
    assert copies is not None
    wt = copies.make(1)
    (wt / "hello.txt").write_text("hello, rung\n")
    (wt / "keep.txt").unlink()
    (wt / "sub").mkdir()
    (wt / "sub" / "x.txt").write_text("x\n")
    (wt / "blob.bin").write_bytes(bytes(range(256)))
    head = _git(repo, "rev-parse", "HEAD")
    index = (repo / ".git" / "index").read_bytes()
    commit, _ = copies.keep(1, "the task", "L.1")
    assert commit is not None
    applied, line = copies.bring_back(1, commit)
    assert applied is True and "4 file(s)" in line
    assert (repo / "hello.txt").read_text() == "hello, rung\n"
    assert not (repo / "keep.txt").exists()
    assert (repo / "sub" / "x.txt").read_text() == "x\n"
    assert (repo / "blob.bin").read_bytes() == bytes(range(256))
    assert _git(repo, "rev-parse", "HEAD") == head  # nothing committed
    assert (repo / ".git" / "index").read_bytes() == index  # nothing staged
    assert private_mode(copies.patch_path()) == 0o600


def test_work_that_no_longer_fits_is_not_applied_and_its_copy_stays(repo):
    copies = Copies.start(repo, "L")
    assert copies is not None
    wt = copies.make(1)
    (wt / "hello.txt").write_text("from the rung\n")
    commit, _ = copies.keep(1, "t", "L.1")
    assert commit is not None
    (repo / "hello.txt").write_text("the person changed it meanwhile\n")
    applied, line = copies.bring_back(1, commit)
    assert applied is False
    assert (repo / "hello.txt").read_text() == "the person changed it meanwhile\n"
    assert str(wt) in line and "nothing was changed" in line
    assert "metabot desks clean L" in line
    assert copies.close(keep=1) == []
    assert wt.exists()  # the work stays where the person can take it
    trees, _ = run_items(repo, "L")
    assert [path for path, _ in trees] == [wt.resolve()]  # and desks clean finds it


def test_closing_removes_every_copy(repo):
    copies = Copies.start(repo, "L")
    assert copies is not None
    one, two = copies.make(1), copies.make(2)
    copies.keep(1, "t", "L.1")
    copies.restore(1)
    assert copies.close() == []
    assert not one.exists() and not two.exists()
    assert "rung-" not in _git(repo, "worktree", "list")
    assert not one.parent.exists()  # no empty folder left behind


def _break_link(wt: Path, repo: Path, how: str) -> None:
    """What a rung's work can do to its copy's .git file."""
    dot = wt / ".git"
    link = dot.read_bytes()
    if how == "edited":  # a search and replace over the copy, as on 10-22
        assert b"rung-1" in link
        dot.write_bytes(link.replace(b"rung-1", b"rung-2"))
    elif how == "pointed at the project":
        dot.write_text(f"gitdir: {repo / '.git'}\n")
    elif how == "a folder":
        dot.unlink()
        dot.mkdir()
        (dot / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        dot.unlink()
        dot.symlink_to(repo / ".git")


LINKS = ["edited", "pointed at the project", "a folder", "a symlink to the project's"]


@pytest.mark.parametrize("how", LINKS)
def test_work_that_changed_its_copy_s_git_file_is_committed_in_the_copy(repo, how):
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    copies = Copies.start(repo, "L")
    assert copies is not None
    wt = copies.make(1)
    link = (wt / ".git").read_bytes()
    (wt / "hello.txt").write_text("from the rung\n")
    _break_link(wt, repo, how)
    before = _state(repo)
    commit, relinked = copies.keep(1, "t", "L.1")
    assert relinked is True
    assert not (wt / ".git").is_symlink() and (wt / ".git").read_bytes() == link
    assert commit is not None and _git(wt, "rev-parse", "HEAD").strip() == commit
    assert _git(wt, "show", f"{commit}:hello.txt") == "from the rung\n"
    assert _state(repo) == before  # the person's HEAD, index and branches: byte for byte
    copies.restore(1)
    assert copies.close() == []


def test_a_copy_whose_git_file_changed_after_its_commit_is_still_removed(repo):
    copies = Copies.start(repo, "L")
    assert copies is not None
    wt = copies.make(1)
    _break_link(wt, repo, "edited")
    assert copies.close() == []
    assert not wt.exists()


# ── judging with a check, and in copies ─────────────────────────────────────


def _receipt(tmp_path: Path, *verdicts: dict) -> Path:
    path = tmp_path / f"r{len(list(tmp_path.glob('r*.json')))}.json"
    path.write_text(json.dumps({"verdicts": list(verdicts)}))
    return path


def _ran(tmp_path, *verdicts, **kw) -> SimpleNamespace:
    base = {"ok": True, "declined": 0, "failed": 0, "limit": None, "error": None,
            "run_id": "L.1", "cost_musd": 0, "cost_source": "local", "tokens": 5}  # fmt: skip
    return SimpleNamespace(**{**base, **kw, "receipt": _receipt(tmp_path, *verdicts)})


def test_a_passing_check_answers_and_a_failing_one_climbs(tmp_path):
    assert judge(_ran(tmp_path), True, check="pass") == (ANSWERED, "")
    outcome, why = judge(_ran(tmp_path), True, check="fail: ask:1 exit 1")
    assert (outcome, why) == (CLIMBED, "its check failed: ask:1 exit 1")


ACCEPT_FAIL = {"check": "declared_acceptance", "result": "fail", "detail": "criteria"}


def test_a_passing_check_outweighs_a_call_that_failed_on_the_way(tmp_path):
    # The bench, 10-22: the model ran a test that failed, fixed the file, and
    # the person's check passed; without a check the failed call still climbs.
    outcome, why = judge(_ran(tmp_path, failed=2), True, check="pass")
    assert outcome == ANSWERED and "2 call(s) on the way did not succeed" in why
    assert judge(_ran(tmp_path, failed=2), True) == (CLIMBED, "2 call(s) did not succeed")


def test_a_passing_check_does_not_answer_for_a_run_that_did_not_finish(tmp_path):
    assert judge(_ran(tmp_path, ok=False, error="boom"), True, check="pass") == (CLIMBED, "boom")
    assert judge(_ran(tmp_path, limit="$0.01"), True, check="pass")[0] == STOPPED
    assert judge(_ran(tmp_path, ACCEPT_FAIL), True, check="pass")[0] == CLIMBED


WROTE = {"check": "scope_allow:c1", "result": "pass", "detail": "allow_write"}


def test_a_failed_rung_that_wrote_climbs_in_a_copy_and_stops_in_place(tmp_path):
    failed = _ran(tmp_path, WROTE, ok=False, error="boom")
    assert judge(failed, True, copied=True) == (CLIMBED, "boom")
    assert judge(failed, True, copied=False)[0] == STOPPED


def test_a_failed_rung_on_an_engine_whose_tools_run_unasked_stops_even_in_a_copy(tmp_path):
    outcome, why = judge(_ran(tmp_path, ok=False, error="boom"), False, copied=True)
    assert outcome == STOPPED and "own tools" in why


def _declined(n: int, detail: str) -> dict:
    return {"check": f"scope_approval:c{n}", "result": "warn", "detail": detail}


NOBODY = _declined(1, "declined: no human present")


def test_in_a_copy_a_decline_with_no_one_to_ask_climbs(tmp_path):
    # The bench, 10-22: a 3B rung's malformed shell calls were declined with
    # no one to ask, and its check failed; claude would have answered.
    rung = _ran(tmp_path, NOBODY, declined=1)
    outcome, why = judge(rung, True, check="fail: ask:1 exit 1", copied=True)
    assert outcome == CLIMBED
    assert why == "its check failed: ask:1 exit 1; 1 call(s) declined with no one to ask"


def test_in_place_a_decline_with_no_one_to_ask_still_stops(tmp_path):
    rung = _ran(tmp_path, NOBODY, declined=1)
    outcome, why = judge(rung, True, check="fail: ask:1 exit 1", copied=False)
    assert outcome == STOPPED and "every rung meets the same gate" in why


@pytest.mark.parametrize(
    "verdicts",
    [
        (_declined(1, "declined"),),  # a person said no
        (NOBODY, _declined(2, "declined")),  # one of them was a person's no
        (_declined(1, "declined: cancelled"),),
        (_declined(1, "declined: approver error"),),
    ],
)
def test_in_a_copy_any_other_decline_still_stops(tmp_path, verdicts):
    rung = _ran(tmp_path, *verdicts, declined=len(verdicts))
    assert judge(rung, True, check="fail: ask:1 exit 1", copied=True)[0] == STOPPED


def test_a_decline_with_no_one_to_ask_on_an_ungated_engine_still_stops(tmp_path):
    rung = _ran(tmp_path, NOBODY, declined=1, ok=False, error="boom")
    outcome, why = judge(rung, False, copied=True)
    assert (outcome, why) == (
        STOPPED,
        "it failed on an engine whose own tools may have changed the project",
    )


# ── the ladder with checks and copies ───────────────────────────────────────


def _writer(
    tmp_path: Path, writes: dict[int, str], here: Path | None = None, ends: dict | None = None
):
    """A rung that writes answer.txt in the folder it was given (else ``here``),
    and ends as ``ends[n]`` says."""
    seen: list[Path | None] = []

    async def run_rung(rung, run_id, meta, cap, where):
        seen.append(where)
        n = meta["rung"]
        if n in writes:
            ((where or here) / "answer.txt").write_text(writes[n] + "\n")
        verdicts = (WROTE,) if n in writes else ()
        return _ran(tmp_path, *verdicts, run_id=run_id, **(ends or {}).get(n, {}))

    return run_rung, seen


TWO = [Rung("openai", "tiny", "local"), Rung("openai", "big", "local")]


def _ladder(repo: Path, run_rung, **kw):
    kw.setdefault("gated", lambda engine: True)
    return asyncio.run(
        run_ladder(TWO, run_rung, cfg=Config(), ladder_id="L", cwd=repo, task="t", **kw)
    )


def test_a_failing_check_climbs_and_only_the_passing_rung_s_work_lands(repo, tmp_path):
    run_rung, seen = _writer(tmp_path, {1: "wrong", 2: "right"})
    copies = Copies.start(repo, "L")
    result = _ladder(repo, run_rung, checks={"ask:1": RIGHT}, copies=copies)
    assert [r.outcome for r in result.rows] == [CLIMBED, ANSWERED]
    assert result.rows[0].check == "fail: ask:1 exit 1" and result.rows[1].check == "pass"
    assert (repo / "answer.txt").read_text() == "right\n"  # rung 1's write never landed
    assert result.applied is True and seen[0] != seen[1]
    assert not any(p.exists() for p in seen if p)  # every copy is gone
    for row in result.rows:  # each rung's checks are sealed as their own run
        assert row.checks_run == f"{row.run_id}.checks"
        assert verify_file(receipt_path(row.checks_run)).valid
    record = load_ladder("L")
    assert record is not None and record["applied"] is True and record["base"] == copies.base


def test_the_check_runs_on_a_rung_whose_calls_failed_and_its_work_lands(repo, tmp_path):
    run_rung, _ = _writer(tmp_path, {1: "right"}, ends={1: {"failed": 1}})
    result = _ladder(repo, run_rung, checks={"ask:1": RIGHT}, copies=Copies.start(repo, "L"))
    assert [r.outcome for r in result.rows] == [ANSWERED]
    assert result.rows[0].check == "pass" and result.applied is True
    assert (repo / "answer.txt").read_text() == "right\n"


def test_a_rung_declined_with_no_one_to_ask_climbs_and_the_next_one_s_work_lands(repo, tmp_path):
    seen: list[Path | None] = []

    async def run_rung(rung, run_id, meta, cap, where):
        seen.append(where)
        n = meta["rung"]
        (where / "answer.txt").write_text("wrong\n" if n == 1 else "right\n")
        if n == 1:
            return _ran(tmp_path, WROTE, NOBODY, run_id=run_id, declined=1)
        return _ran(tmp_path, WROTE, run_id=run_id)

    result = _ladder(repo, run_rung, checks={"ask:1": RIGHT}, copies=Copies.start(repo, "L"))
    assert [r.outcome for r in result.rows] == [CLIMBED, ANSWERED]
    assert "declined with no one to ask" in result.rows[0].why
    assert (repo / "answer.txt").read_text() == "right\n" and result.applied is True


def test_a_rung_that_pointed_its_copy_at_the_project_climbs_and_the_project_is_untouched(
    repo, tmp_path
):
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    before = _state(repo)

    async def run_rung(rung, run_id, meta, cap, where):
        n = meta["rung"]
        (where / "answer.txt").write_text("wrong\n" if n == 1 else "right\n")
        if n == 1:
            _break_link(where, repo, "pointed at the project")
        return _ran(tmp_path, WROTE, run_id=run_id)

    lines: list[str] = []
    result = _ladder(
        repo,
        run_rung,
        checks={"ask:1": RIGHT},
        copies=Copies.start(repo, "L"),
        on_line=lines.append,
    )
    assert [r.outcome for r in result.rows] == [CLIMBED, ANSWERED]
    assert [r.relinked for r in result.rows] == [True, False]
    assert "rung 1's work changed its copy's .git file; MetaBot put it back" in lines
    assert (repo / "answer.txt").read_text() == "right\n" and result.applied is True
    after = _state(repo)
    for key in ("head", "refs", "index"):  # nothing committed, nothing unstaged
        assert after[key] == before[key]
    record = load_ladder("L")
    assert record is not None and record["rungs"][0]["relinked"] is True


def test_a_rung_whose_work_cannot_be_committed_in_its_copy_climbs(repo, tmp_path, monkeypatch):
    real = ladder_copies.commit_desk

    def commit_desk(wt, desk, *args, **kw):
        if desk == "rung-1":
            raise DeskGitError("cannot commit desk rung-1's work: it would not")
        return real(wt, desk, *args, **kw)

    monkeypatch.setattr(ladder_copies, "commit_desk", commit_desk)
    run_rung, _ = _writer(tmp_path, {1: "right", 2: "right"})
    result = _ladder(repo, run_rung, checks={"ask:1": RIGHT}, copies=Copies.start(repo, "L"))
    assert [r.outcome for r in result.rows] == [CLIMBED, ANSWERED]
    unkept = "its work could not be committed in its copy"
    assert result.rows[0].why == f"{unkept}: cannot commit desk rung-1's work: it would not"
    assert result.rows[0].check is None and result.rows[0].commit is None  # no check, no commit
    assert (repo / "answer.txt").read_text() == "right\n" and result.applied is True


def test_no_check_runs_after_a_rung_that_passed_a_limit(repo, tmp_path):
    run_rung, _ = _writer(tmp_path, {1: "right"}, ends={1: {"limit": "$0.01"}})
    result = _ladder(repo, run_rung, checks={"ask:1": RIGHT}, copies=Copies.start(repo, "L"))
    assert [r.outcome for r in result.rows] == [STOPPED]
    assert result.rows[0].check is None and result.rows[0].checks_run is None
    assert not (repo / "answer.txt").exists()  # nothing was brought back


def test_in_place_the_check_runs_in_the_person_s_folder(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    run_rung, seen = _writer(tmp_path, {1: "right"}, here=folder)
    result = _ladder(folder, run_rung, checks={"ask:1": RIGHT})
    assert seen == [None]  # no copy: the rung works in the person's folder
    assert result.rows[0].check == "pass" and result.answered


def test_a_rung_that_changed_nothing_has_nothing_to_bring_back(repo, tmp_path):
    run_rung, _ = _writer(tmp_path, {})
    result = _ladder(repo, run_rung, copies=Copies.start(repo, "L"))
    assert result.answered and result.applied is None


# ── ask --ladder --check, end to end ────────────────────────────────────────


def test_check_goes_with_ladder(capsys):
    assert main(["ask", "--check", RIGHT, "hi"]) == 2
    assert "--check goes with --ladder" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["rm -rf build-output-that-is-not-here", "grep a\nb"])
def test_a_destructive_or_multi_line_check_is_refused(bad, capsys):
    assert main(["ask", "--ladder", "--check", bad, "hi"]) == 2
    assert "these checks never run" in capsys.readouterr().err


def test_ask_ladder_check_climbs_in_copies_and_lands_the_right_work(
    chat, repo, monkeypatch, capsys
):
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    fake = chat([
        call("write_file", {"path": "answer.txt", "content": "wrong\n"}), answer("done"),
        call("write_file", {"path": "answer.txt", "content": "right\n"}), answer("done"),
    ])  # fmt: skip
    save_config(
        Config(
            openai_base_url=fake.url,
            ladder=[{"engine": "openai", "model": "tiny"}, {"engine": "openai", "model": "big"}],
        )
    )
    argv = ["ask", "--ladder", "--allow-write", "--json", "--check", RIGHT, "--cwd", str(repo),
            "write the answer"]  # fmt: skip
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    ladder = out["ladder"]
    assert [r["outcome"] for r in ladder["rungs"]] == [CLIMBED, ANSWERED]
    assert "its check failed" in ladder["rungs"][0]["why"]
    assert ladder["applied"] is True
    assert (repo / "answer.txt").read_text() == "right\n"
    for row in ladder["rungs"]:
        assert verify_file(receipt_path(row["run_id"])).valid
        assert verify_file(receipt_path(row["checks_run"])).valid
    assert _git(repo, "status", "--porcelain").strip() == "?? answer.txt"
