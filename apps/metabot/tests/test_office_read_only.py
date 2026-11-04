"""Read-only desks: a desk with ``read_only: true`` takes its turn, files nothing, and hands on."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from metabot.desks import list_desks
from metabot.office import OfficeError, desk_problem, load_office
from metabot.office_run import office_journal_path
from metabot.paths import run_path
from metabot.receipts import journal_matches, verify_file
from test_checks import _trusted
from test_office_advisor import Advisor
from test_office_run import DeskEngine, _build, _git, _relay, _repo

READ_ONLY = {
    "plan": {"role": "roles/plan.md", "harness": "claude", "read_only": True},
    "build": {"role": "roles/build.md", "harness": "codex"},
    "review": {"role": "roles/review.md", "harness": "cursor", "read_only": True},
}


def _tamper(answer: str):
    def act(job) -> str:
        (Path(job.cwd) / "app.txt").write_text("changed by a read-only desk\n", encoding="utf-8")
        return answer

    return act


def _engines() -> dict:
    return {
        "claude": DeskEngine("claude", act=_tamper("PLAN: write feature.txt")),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=_tamper("REVIEW: fine")),
    }


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").strip()


def test_read_only_is_read_from_the_office(tmp_path):
    office = load_office(_repo(tmp_path, READ_ONLY))
    assert office.desks["plan"].read_only and office.desks["review"].read_only
    assert not office.desks["build"].read_only


def test_read_only_must_be_true_or_false(tmp_path):
    desks = {"plan": {"role": "roles/plan.md", "harness": "claude", "read_only": "yes"}}
    with pytest.raises(OfficeError, match=r"desks\.plan\.read_only"):
        load_office(_repo(tmp_path, desks))


def test_a_read_only_desk_has_no_fan(tmp_path):
    desks = {"plan": {"role": "roles/plan.md", "harness": "claude", "read_only": True, "fan": 2}}
    office = load_office(_repo(tmp_path, desks))
    assert "read-only desk files nothing, so it has no fan" in str(
        desk_problem(office.desks["plan"])
    )


async def test_a_read_only_desk_runs_read_only_files_nothing_and_hands_on(tmp_path, home):
    root = _repo(tmp_path, READ_ONLY)
    base = _head(root)
    engines = _engines()
    result = await _relay(root, engines, allow_write=True)
    assert result.ok, [(r.label, r.error) for r in result.rows]
    plan, build, review = result.rows
    assert [r.label for r in result.rows] == ["plan", "build", "review"]

    [plan_job] = engines["claude"].jobs
    [build_job] = engines["codex"].jobs
    [review_job] = engines["cursor"].jobs
    assert plan_job.read_only and review_job.read_only and not build_job.read_only

    # it files nothing: no branch, no commit, and what it changed is put back
    for row, job in ((plan, plan_job), (review, review_job)):
        assert row.branch is None and row.commit is None and row.stat is None
        wt = Path(job.cwd)
        assert (wt / "app.txt").read_text(encoding="utf-8") == "v1\n"
        detached = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"])
        assert detached.returncode != 0
        assert verify_file(row.receipt).valid and journal_matches(row.run_id)

    # the next desk starts where the read-only desk started, with its answer in the inbox
    assert _head(Path(plan_job.cwd)) == base
    assert _git(root, "rev-parse", f"{build.branch}~1").strip() == base
    inbox = Path(build_job.cwd) / ".metabot" / "inbox"
    assert (inbox / "plan.md").read_text(encoding="utf-8") == "PLAN: write feature.txt"
    assert _head(Path(review_job.cwd)) == build.commit  # it reads the build desk's work

    # it is a handoff, so the next desk's parent is the read-only desk
    assert _journal(build.run_id)["meta"]["parent"] == plan.run_id
    assert _journal(review.run_id)["meta"]["parent"] == build.run_id
    assert _journal(plan.run_id)["meta"]["read_only"] is True

    # only the desk that filed work is offered, and only it is a desk to pick
    assert result.offer == ["build"]
    office = result.office_run_id
    assert [d.label for d in list_desks(root) if d.run_id == office] == ["build"]
    record = json.loads(office_journal_path(office).read_text(encoding="utf-8"))
    assert [d["read_only"] for d in record["desks"]] == [True, False, True]


async def test_a_read_only_desk_is_asked_even_for_what_the_desks_may_do(tmp_path):
    root = _repo(tmp_path, {"plan": READ_ONLY["plan"], "build": READ_ONLY["build"]})
    asks = Advisor("claude", act=lambda job: "PLAN")
    engines = {**_engines(), "claude": asks}
    result = await _relay(root, engines, allow_write=True)
    assert result.ok, [(r.label, r.error) for r in result.rows]
    [reply] = asks.replies  # the person allowed writes; a read-only desk's are asked, and declined
    assert reply["allow"] is False


async def test_checks_do_not_run_on_a_read_only_desk(tmp_path):
    desks = {"plan": READ_ONLY["plan"], "build": READ_ONLY["build"]}
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"}, desks=desks)
    result = await _relay(root, _engines(), checks=True)
    plan, build = result.rows
    assert result.ok, [(r.label, r.error) for r in result.rows]
    assert plan.checks is None and plan.checks_receipt is None
    assert build.checks == "pass"
