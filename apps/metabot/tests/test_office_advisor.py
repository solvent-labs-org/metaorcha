"""Advisor desks: they review another desk's work, read-only, and file nothing."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from conftest import not_on_path
from metabot import office_run
from metabot.approvals import DenyAll
from metabot.config import Config, save_config
from metabot.desks import list_desks
from metabot.events import ErrorEvent, Final, Started
from metabot.office import OfficeError, desk_engine, desk_problem, load_office
from metabot.office_run import office_journal_path, run_fan
from metabot.paths import run_path
from metabot.receipts import journal_matches, verify_file
from test_office_run import TASK, DeskEngine, _build, _cli, _git, _relay, _repo

ADVISED = {
    "build": {"role": "roles/build.md", "harness": "codex"},
    "qa": {"role": "roles/qa.md", "harness": "cursor", "advises": ["build"]},
    "ship": {"role": "roles/ship.md", "harness": "claude"},
}


class Advisor(DeskEngine):
    """Reviews, and asks the gate about one write while it does."""

    def __init__(self, name: str, act=None, order: list | None = None) -> None:
        super().__init__(name, act=act)
        self.order = order
        self.replies: list[dict] = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        if self.order is not None:
            self.order.append(Path(job.cwd).name)
        yield Started(f"s-{self.name}", job.model)
        msg = {"op": "approve", "call_id": "w1", "tool": "fake/Edit", "cls": "write"}
        self.replies.append(await approve({**msg, "preview": "edit app.txt"}))
        if self.fail:
            yield ErrorEvent(self.fail)
        yield Final(self.act(job) if self.act else "ADVICE: add a test", f"s-{self.name}")


class Ordered(DeskEngine):
    def __init__(self, name: str, order: list, **kw) -> None:
        super().__init__(name, **kw)
        self.order = order

    async def run(self, job, *, board=None, approve):
        self.order.append(Path(job.cwd).name)
        async for event in super().run(job, board=board, approve=approve):
            yield event


def _engines(order: list | None = None, advise=None) -> dict:
    order = [] if order is None else order
    return {
        "codex": Ordered("codex", order, act=_build),
        "cursor": Advisor("cursor", act=advise, order=order),
        "claude": Ordered("claude", order, act=lambda job: "SHIPPED"),
    }


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").strip()


async def test_advisor_runs_read_only_after_the_desk_it_advises(tmp_path):
    order: list = []
    engines = _engines(order)
    root = _repo(tmp_path, ADVISED)
    base = _head(root)
    result = await _relay(root, engines, allow_write=True)
    assert result.ok
    build, qa, ship = result.rows
    assert [r.label for r in result.rows] == ["build", "qa.on-build", "ship"]
    assert order == ["build", "qa.on-build", "ship"]
    [job] = engines["cursor"].jobs
    assert job.read_only and not engines["codex"].jobs[0].read_only
    assert _head(Path(job.cwd)) == build.commit  # it reviews the build desk's commit
    assert "you advise the build desk" in job.prompt
    assert f"git diff {base} HEAD" in job.prompt
    assert (Path(job.cwd) / ".metabot" / "inbox" / "build.md").read_text() == "BUILT feature.txt"
    [reply] = engines["cursor"].replies  # every write is asked, though the desks may write
    assert reply["allow"] is False
    assert qa.advises == "build" and qa.ok and qa.answer == "ADVICE: add a test"
    assert verify_file(qa.receipt).valid and journal_matches(qa.run_id)
    meta = _journal(qa.run_id)["meta"]
    assert (meta["advises"], meta["parent"]) == ("build", build.run_id)
    assert _journal(ship.run_id)["meta"]["parent"] == build.run_id  # advice is not a handoff


async def test_advisor_files_nothing(tmp_path, capsys):
    def tamper(job):
        (Path(job.cwd) / "app.txt").write_text("changed by the advisor\n", encoding="utf-8")
        (Path(job.cwd) / "notes.txt").write_text("a note\n", encoding="utf-8")
        return "ADVICE: changed things anyway"

    root = _repo(tmp_path, ADVISED)
    engines = _engines(advise=tamper)
    result = await _relay(root, engines)
    _, qa, ship = result.rows
    assert qa.commit is None and qa.branch is None and qa.stat is None
    wt = Path(engines["cursor"].jobs[0].cwd)
    assert _git(wt, "status", "--porcelain") == "?? .metabot/\n"  # put back; the inbox stays
    assert (wt / "app.txt").read_text(encoding="utf-8") == "v1\n"
    detached = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"], check=False)
    assert detached.returncode != 0  # no branch at all
    assert "app.txt" not in _git(root, "diff", "--name-only", ship.branch, result.rows[0].branch)
    office = result.office_run_id
    assert [d.label for d in list_desks(root) if d.run_id == office] == ["build", "ship"]
    code, out, err = _cli(capsys, "desks", "list", "--dir", str(root))
    assert code == 0 and "qa.on-build" not in out
    code, out, err = _cli(capsys, "desks", "pick", f"{office}/qa.on-build", "--dir", str(root))
    assert code != 0
    code, out, err = _cli(capsys, "desks", "clean", office, "--dir", str(root), "--yes")
    assert code == 0, out + err
    assert str(wt) in out and not wt.exists()


async def test_advisor_artefact_goes_to_the_next_desk_and_the_person(tmp_path):
    root = _repo(tmp_path, ADVISED)
    engines = _engines()
    result = await _relay(root, engines)
    qa = result.rows[1]
    assert (run_path(qa.run_id) / "artefact.md").read_text() == "ADVICE: add a test"
    [ship_job] = engines["claude"].jobs
    inbox = Path(ship_job.cwd) / ".metabot" / "inbox"
    assert (inbox / "qa.on-build.md").read_text(encoding="utf-8") == "ADVICE: add a test"
    assert str(inbox / "qa.on-build.md") in ship_job.prompt
    assert "and their advisors, left their answers" in ship_job.prompt


def test_advice_is_printed_for_the_person(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path, ADVISED)
    monkeypatch.setattr(office_run, "get_engine", lambda name: _engines()[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    lines = out.splitlines()
    at = lines.index("advice of qa on build:")
    assert lines[at + 1] == "ADVICE: add a test"
    assert any(line.strip().startswith("qa.on-build") and "valid" in line for line in lines)
    assert not any("pick" in line and "qa.on-build" in line for line in lines)
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    code, out, err = _cli(capsys, "receipts", "show", office_id)
    [line] = [x for x in out.splitlines() if x.strip().startswith("qa.on-build")]
    assert "ok" in line and "valid" in line


COACHED = {
    "build": {"role": "roles/build.md", "harness": "codex"},
    "ship": {"role": "roles/ship.md", "harness": "claude"},
    "coach": {"role": "roles/coach.md", "harness": "cursor", "advises": True},
}


async def test_advises_you_only_reports(tmp_path):
    root = _repo(tmp_path, COACHED)
    base = _head(root)
    order: list = []
    engines = _engines(order, advise=lambda job: "ADVICE: tell the owner")
    result = await _relay(root, engines)
    assert order == ["build", "ship", "coach.on-you"]
    ship, coach = result.rows[1], result.rows[2]
    [job] = engines["cursor"].jobs
    assert _head(Path(job.cwd)) == _head(Path(engines["claude"].jobs[0].cwd))
    assert coach.advises == "you" and "you advise the person running the office" in job.prompt
    assert f"git diff {base} HEAD" in job.prompt  # the whole office's work
    assert _journal(coach.run_id)["meta"]["parent"] == ship.run_id
    record = json.loads(office_journal_path(result.office_run_id).read_text(encoding="utf-8"))
    assert record["advisors"] == {"you": ["coach"]}


def test_advice_for_you_is_printed(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path, COACHED)
    monkeypatch.setattr(office_run, "get_engine", lambda name: _engines()[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    lines = out.splitlines()
    assert "advice of coach on you:" in lines
    assert "answer of ship:" in lines  # the last desk's answer, not the advisor's
    assert any(line.startswith(f"next: metabot desks pick {office_id}/ship ") for line in lines)


class Asks(DeskEngine):
    """Asks the gate about one write, then builds."""

    def __init__(self, name: str) -> None:
        super().__init__(name, act=_build)
        self.replies: list[dict] = []

    async def run(self, job, *, board=None, approve):
        msg = {"op": "approve", "call_id": "w1", "tool": "fake/Edit", "cls": "write"}
        self.replies.append(await approve({**msg, "preview": "edit app.txt"}))
        async for event in super().run(job, board=board, approve=approve):
            yield event


async def test_an_advisor_is_asked_even_for_what_the_desks_may_do(tmp_path):
    engines = {**_engines(), "codex": Asks("codex")}
    result = await _relay(_repo(tmp_path, ADVISED), engines, allows=["Edit"])
    assert result.ok
    [desk] = engines["codex"].replies
    [advisor] = engines["cursor"].replies
    assert desk["allow"] is True and advisor["allow"] is False


async def test_advice_for_you_waits_for_every_desk(tmp_path):
    desks = {
        "build": {"role": "roles/build.md", "harness": "codex"},
        "coach": {"role": "roles/coach.md", "harness": "cursor", "advises": True},
    }
    engines = _engines()
    engines["codex"].fail = "codex exited 1"
    result = await _relay(_repo(tmp_path, desks), engines)
    assert [r.label for r in result.rows] == ["build"] and engines["cursor"].jobs == []


async def test_a_failed_advisor_does_not_stop_the_relay(tmp_path):
    engines = _engines()
    engines["cursor"].fail = "cursor exited 1"
    result = await _relay(_repo(tmp_path, ADVISED), engines)
    build, qa, ship = result.rows
    assert build.ok and not qa.ok and ship.ok and not result.ok
    inbox = Path(engines["claude"].jobs[0].cwd) / ".metabot" / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["build.md"]  # no advice was handed on


async def test_an_advisor_reviews_every_good_copy(tmp_path):
    desks = {
        "build": {"role": "roles/build.md", "harness": "codex", "fan": 2},
        "qa": {"role": "roles/qa.md", "harness": "cursor", "advises": ["build"]},
    }
    root = _repo(tmp_path, desks)
    base = _head(root)
    engines = _engines()
    result = await _relay(root, engines)
    labels = [r.label for r in result.rows]
    assert labels == ["build.1", "build.2", "qa.on-build"]
    [job] = engines["cursor"].jobs
    assert _head(Path(job.cwd)) == base  # where the copies started
    for row in result.rows[:2]:
        assert f"- {row.branch}" in job.prompt
    assert "git diff HEAD <branch>" in job.prompt
    assert _journal(result.rows[2].run_id)["meta"]["parent"] == ",".join(
        r.run_id for r in result.rows[:2]
    )


async def test_an_advisor_is_not_run_on_its_own(tmp_path):
    with pytest.raises(OfficeError, match="an advisor runs after the desks it advises"):
        await _relay(_repo(tmp_path, ADVISED), _engines(), desks=["build", "qa"])


async def test_an_advisor_that_cannot_run_refuses_the_run_unless_left_out(tmp_path):
    desks = {
        **ADVISED,
        "aider": {
            "role": "roles/aider.md",
            "harness": "custom",
            "command": "aider",
            "advises": True,
        },
    }
    root = _repo(tmp_path, desks)
    engines = _engines()
    with pytest.raises(OfficeError) as caught:
        await _relay(root, engines)
    assert caught.value.where == "advisors" and "--no-advisors" in str(caught.value)
    assert engines["codex"].jobs == []
    result = await _relay(root, engines, advisors=False)
    assert [r.label for r in result.rows] == ["build", "ship"] and engines["cursor"].jobs == []


async def test_an_advisor_whose_command_is_not_on_path_refuses_the_run_unless_left_out(
    tmp_path, monkeypatch
):
    not_on_path(monkeypatch, "cursor-agent")
    root = _repo(tmp_path, ADVISED)
    engines = _engines()
    with pytest.raises(OfficeError) as caught:
        await _relay(root, engines)
    assert caught.value.where == "advisors"
    assert "qa: cursor-agent is not on PATH" in str(caught.value)
    assert "--no-advisors" in str(caught.value)
    assert engines["codex"].jobs == []
    result = await _relay(root, engines, advisors=False)
    assert [r.label for r in result.rows] == ["build", "ship"] and engines["cursor"].jobs == []


async def test_advisors_do_not_run_in_a_fan(tmp_path):
    engines = _engines()
    result = await run_fan(
        load_office(_repo(tmp_path, ADVISED)),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    assert sorted(r.label for r in result.rows) == ["build", "ship"]
    assert engines["cursor"].jobs == []


def test_an_any_advisor_runs_on_the_review_role(tmp_path):
    desks = {
        "build": {"role": "roles/build.md", "harness": "any"},
        "qa": {"role": "roles/qa.md", "harness": "any", "advises": ["build"]},
    }
    office = load_office(_repo(tmp_path, desks))
    roster = {"build": {"engine": "claude"}, "review": {"engine": "codex", "model": "m-review"}}
    assert desk_engine(office.desks["qa"], roster) == ("codex", "m-review")
    assert desk_engine(office.desks["build"], roster) == ("claude", None)
    assert desk_engine(office.desks["qa"], {"build": {"engine": "claude"}}) == ("claude", None)


def test_an_advisor_has_no_fan(tmp_path):
    desks = {**ADVISED, "qa": {**ADVISED["qa"], "fan": 2}}
    office = load_office(_repo(tmp_path, desks))
    assert "an advisor runs once for each desk it advises" in desk_problem(office.desks["qa"])


class Blocked(DeskEngine):
    """Changes a file, then waits until it is cancelled."""

    def __init__(self, name: str, started: asyncio.Event) -> None:
        super().__init__(name)
        self.started = started

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        (Path(job.cwd) / "app.txt").write_text("half done\n", encoding="utf-8")
        self.started.set()
        await asyncio.Event().wait()
        yield Final("never", f"s-{self.name}")


async def test_a_cancelled_advisor_is_sealed_and_put_back(tmp_path, home):
    started = asyncio.Event()
    engines = {**_engines(), "cursor": Blocked("cursor", started)}
    task = asyncio.ensure_future(_relay(_repo(tmp_path, ADVISED), engines))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [path] = (home / "offices").glob("*.json")
    rows = {d["label"]: d for d in json.loads(path.read_text(encoding="utf-8"))["desks"]}
    assert set(rows) == {"build", "qa.on-build"}
    qa = rows["qa.on-build"]
    assert qa["error"] == "cancelled" and not qa["ok"] and verify_file(qa["receipt"]).valid
    wt = Path(engines["cursor"].jobs[0].cwd)
    assert (wt / "app.txt").read_text(encoding="utf-8") == "v1\n"


def test_a_relay_with_advisors_through_the_cli_and_without(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path, ADVISED)
    monkeypatch.setattr(office_run, "get_engine", lambda name: _engines()[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--no-advisors", "-q", TASK)
    assert code == 0, err
    assert "qa.on-build" not in out and "advice of" not in out
    save_config(Config())
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--desks", "qa", TASK)
    assert code == 2 and "an advisor runs after the desks it advises" in err


def test_an_answer_or_advice_whose_calls_were_declined_says_so(tmp_path, capsys, monkeypatch):
    """The line that says calls were declined names the desk, and shows even when quiet."""
    root = _repo(tmp_path, COACHED)
    engines = {**_engines(), "claude": Asks("claude")}
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    caveat = (
        "1 call was declined. The answer may say otherwise; the receipt's steps are what happened."
    )
    said = [line for line in err.splitlines() if "was declined" in line]
    assert said == [f"[metabot] coach.on-you: {caveat}", f"[metabot] ship: {caveat}"]
    assert "answer of ship:" in out.splitlines()
