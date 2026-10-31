"""Loops: with rounds, a desk whose checks fail goes again, with what the checks said."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from metabot.config import Config, save_config
from metabot.connections import ConnectionStore
from metabot.events import Final, Started, Usage
from metabot.office import OfficeError
from metabot.office_run import office_journal_path
from metabot.paths import run_path
from metabot.receipts import journal_matches, verify_file
from test_checks import BUILD, RELAY, _trusted
from test_office_run import TASK, DeskEngine, _cli, _git, _relay

WANT = 'grep -qx hello feature.txt || { echo "want hello, got $(cat feature.txt)" >&2; exit 1; }'
LOOP = {"build": {"role": "roles/build.md", "harness": "claude", "model": "m-loop"}}
PRICES = {"m-loop": {"in": 1.0, "out": 2.0}}  # 100 prompt tokens: 100 micro-dollars a round


class Fixer(DeskEngine):
    """Writes the wrong greeting until its ``right``-th run, then the right one."""

    def __init__(self, name: str = "codex", right: int = 2, same: bool = False, usage=None):
        super().__init__(name)
        self.right = right
        self.same = same
        self.usage = usage

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        n = len(self.jobs)
        yield Started(f"s-{n}", job.model)
        if self.usage:
            yield Usage(self.usage)
        text = "hello" if n >= self.right else ("wrong" if self.same else f"wrong {n}")
        (Path(job.cwd) / "feature.txt").write_text(text + "\n", encoding="utf-8")
        yield Final(f"wrote {text}", f"s-{n}")


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


def _record(office_run_id: str) -> dict:
    return json.loads(office_journal_path(office_run_id).read_text(encoding="utf-8"))


async def test_a_desk_goes_again_with_what_the_checks_said_until_they_pass(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    fixer = Fixer()
    result = await _relay(root, {"codex": fixer}, checks=True, rounds=3)
    assert result.ok, [(r.run_id, r.error) for r in result.rows]
    first, second = result.rows
    office = result.office_run_id
    assert (first.run_id, second.run_id) == (f"{office}.build", f"{office}.build.r2")
    assert (first.round, second.round) == (1, 2)
    assert first.next_round == second.run_id and second.next_round is None
    assert first.checks.startswith("fail") and not first.ok
    assert second.checks == "pass" and second.ok
    assert second.stopped == "the checks passed in round 2"
    assert first.branch == second.branch  # one desk, one branch: desks pick finds it by label
    assert _git(root, "show", f"{second.branch}:feature.txt") == "hello\n"
    assert _git(root, "rev-parse", f"{second.commit}~1").strip() == first.commit

    job1, job2 = fixer.jobs
    assert job1.cwd == job2.cwd  # the same worktree, from where round 1 left it
    said = Path(job2.cwd) / ".metabot" / "inbox" / "build.checks.r1.md"
    assert "want hello, got wrong 1" in said.read_text(encoding="utf-8")
    assert "ready:tests" in said.read_text(encoding="utf-8")
    assert "round 2 of 3" in job2.prompt and str(said) in job2.prompt
    assert "round" not in job1.prompt.lower()

    meta = _journal(second.run_id)["meta"]
    assert (meta["parent"], meta["round"]) == (first.run_id, 2)
    assert verify_file(second.receipt).valid and journal_matches(second.run_id)
    assert verify_file(second.checks_receipt).valid
    assert result.offer == ["build"]
    record = _record(office)
    assert record["rounds"] == 3
    assert [d["run_id"] for d in record["desks"]] == [first.run_id, second.run_id]
    assert record["desks"][0]["next_round"] == second.run_id


async def test_the_rounds_run_out(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    result = await _relay(root, {"codex": Fixer(right=99)}, checks=True, rounds=2)
    assert not result.ok
    assert [r.round for r in result.rows] == [1, 2]
    assert result.rows[-1].stopped == "2 rounds used; the checks still fail"
    assert result.offer == []


async def test_a_round_that_changes_no_file_stops_the_loop(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    fixer = Fixer(right=99, same=True)
    result = await _relay(root, {"codex": fixer}, checks=True, rounds=5)
    assert not result.ok and len(fixer.jobs) == 2
    last = result.rows[-1]
    assert last.round == 2 and not last.ok
    assert last.stopped == "round 2 changed no file; the checks still fail"


class Talker(Fixer):
    """Says it did the work and changes no file, until its ``right``-th run."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        n = len(self.jobs)
        yield Started(f"s-{n}", job.model)
        if n >= self.right:
            (Path(job.cwd) / "feature.txt").write_text("hello\n", encoding="utf-8")
        yield Final("Done: I updated feature.txt", f"s-{n}")


async def test_a_first_round_that_changes_no_file_is_judged_and_goes_again(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    talker = Talker(right=2)
    result = await _relay(root, {"codex": talker}, checks=True, rounds=3)
    assert result.ok, [(r.run_id, r.error, r.stopped) for r in result.rows]
    first, second = result.rows
    assert first.commit is None and first.checks.startswith("fail")
    assert second.checks == "pass" and second.stopped == "the checks passed in round 2"
    said = Path(talker.jobs[1].cwd) / ".metabot" / "inbox" / "build.checks.r1.md"
    text = said.read_text(encoding="utf-8")
    assert "Your round 1 changed no file" in text and "want hello" in text
    assert verify_file(first.checks_receipt).valid
    assert result.offer == ["build"]


async def test_a_first_round_that_changes_no_file_where_the_checks_pass_runs_once(tmp_path):
    root = _trusted(tmp_path, {"tests": "true"}, desks=BUILD)
    talker = Talker(right=99)
    result = await _relay(root, {"codex": talker}, checks=True, rounds=3)
    [row] = result.rows
    assert len(talker.jobs) == 1 and row.checks == "pass" and row.commit is None


async def test_without_rounds_a_desk_that_changes_no_file_runs_no_check(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    talker = Talker(right=99)
    result = await _relay(root, {"codex": talker}, checks=True)
    [row] = result.rows
    assert len(talker.jobs) == 1 and row.checks is None and row.checks_receipt is None


async def test_an_error_is_never_tried_again(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    broken = DeskEngine("codex", fail="the engine broke")
    result = await _relay(root, {"codex": broken}, checks=True, rounds=3)
    [row] = result.rows
    assert not row.ok and len(broken.jobs) == 1 and row.round is None


async def test_without_rounds_a_failing_desk_runs_once_as_before(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    fixer = Fixer()
    result = await _relay(root, {"codex": fixer}, checks=True)
    [row] = result.rows
    assert row.run_id == f"{result.office_run_id}.build" and len(fixer.jobs) == 1
    assert (row.round, row.next_round, row.stopped) == (None, None, None)
    assert "round" not in _journal(row.run_id)["meta"]
    assert "rounds" not in _record(result.office_run_id)


async def test_rounds_need_the_checks(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    with pytest.raises(OfficeError, match="rounds"):
        await _relay(root, {"codex": Fixer()}, rounds=2)


async def test_the_next_desk_takes_the_last_round(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=RELAY)
    review = DeskEngine("cursor", act=lambda job: "REVIEW: fine")
    result = await _relay(root, {"codex": Fixer(), "cursor": review}, checks=True, rounds=3)
    assert result.ok, [(r.run_id, r.error) for r in result.rows]
    first, second, reviewed = result.rows
    assert reviewed.desk == "review" and reviewed.round is None
    assert _journal(reviewed.run_id)["meta"]["parent"] == second.run_id
    [job] = review.jobs
    assert (Path(job.cwd) / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    inbox = Path(job.cwd) / ".metabot" / "inbox"
    assert (inbox / "build.md").read_text(encoding="utf-8") == "wrote hello"
    assert not (inbox / "build.checks.r1.md").exists()  # what the checks said was for the desk


async def test_what_the_checks_said_is_scrubbed_of_connection_secrets(tmp_path):
    secret = "sk-test-0123456789abcdef"
    ConnectionStore().add(name="api", transport="http", url="https://api.example", token=secret)
    leak = f"grep -qx hello feature.txt || {{ echo 'token {secret}' >&2; exit 1; }}"
    root = _trusted(tmp_path, {"tests": leak}, desks=BUILD)
    fixer = Fixer()
    # the connection is never started: its secret is redacted all the same
    result = await _relay(root, {"codex": fixer}, checks=True, rounds=2, connections=False)
    assert result.ok
    said = Path(fixer.jobs[1].cwd) / ".metabot" / "inbox" / "build.checks.r1.md"
    assert "token " in said.read_text(encoding="utf-8")
    assert secret not in said.read_text(encoding="utf-8")
    assert secret not in fixer.jobs[1].prompt


class Stalls(Fixer):
    """Writes the wrong greeting, then stalls in round 2 until it is stopped."""

    def __init__(self) -> None:
        super().__init__(right=99)
        self.waiting = asyncio.Event()

    async def run(self, job, *, board=None, approve):
        if len(self.jobs) == 1:
            self.jobs.append(job)
            yield Started("s-2", job.model)
            self.waiting.set()
            await asyncio.sleep(30)
        async for event in super().run(job, board=board, approve=approve):
            yield event


async def test_a_stop_in_a_later_round_seals_it_and_leaves_the_round_before(tmp_path, home):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    stalls = Stalls()
    task = asyncio.ensure_future(_relay(root, {"codex": stalls}, checks=True, rounds=3))
    await asyncio.wait_for(stalls.waiting.wait(), 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    [path] = (home / "offices").glob("*.json")
    first, second = json.loads(path.read_text(encoding="utf-8"))["desks"]
    assert first["next_round"] == second["run_id"] and first["checks"].startswith("fail")
    assert second["round"] == 2 and second["error"] == "cancelled" and not second["ok"]
    assert verify_file(second["receipt"]).valid
    wt = Path(stalls.jobs[0].cwd)
    assert _git(wt, "status", "--porcelain").strip() == "?? .metabot/"
    assert _git(wt, "rev-parse", "HEAD").strip() == first["commit"]


# ── money ───────────────────────────────────────────────────────────────────


async def test_spend_is_summed_over_rounds_and_the_cap_stops_the_loop(tmp_path):
    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    fixer = Fixer("claude", right=99, usage={"prompt_tokens": 100, "completion_tokens": 0})
    result = await _relay(root, {"claude": fixer}, checks=True, rounds=5, max_cost_musd=200)
    assert not result.ok and len(fixer.jobs) == 2
    assert [r.cost_musd for r in result.rows] == [100, 100]
    assert result.rows[-1].stopped == "the office's $0.0002 is spent"
    record = _record(result.office_run_id)
    assert (record["max_cost_musd"], record["spent_musd"]) == (200, 200)


async def test_each_round_runs_under_what_is_left_of_the_cap(tmp_path):
    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    fixer = Fixer("claude", right=99, usage={"prompt_tokens": 100, "completion_tokens": 0})
    result = await _relay(root, {"claude": fixer}, checks=True, rounds=5, max_cost_musd=150)
    assert len(fixer.jobs) == 2
    last = result.rows[-1]
    assert not last.ok and "limit: $0.00005" in str(last.error)


async def test_a_desk_with_no_price_is_refused_under_a_cap_before_any_worktree(tmp_path, home):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)  # codex runs on a plan: no price
    with pytest.raises(OfficeError, match=r"max_cost.*build.*prices"):
        await _relay(root, {"codex": Fixer()}, checks=True, rounds=2, max_cost_musd=1_000_000)
    assert not (home / "desks").exists() or not any((home / "desks").iterdir())


# ── the command line ────────────────────────────────────────────────────────


def test_until_checks_through_the_cli(tmp_path, capsys, monkeypatch):
    from metabot import office_run

    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    fixer = Fixer()
    monkeypatch.setattr(office_run, "get_engine", lambda name: fixer)
    code, out, err = _cli(
        capsys, "office", "run", "--dir", str(root), "-q", "--until-checks", "--rounds", "2", TASK
    )
    assert code == 0, err
    assert "build r2" in out and "the checks passed in round 2" in out


def test_rounds_without_until_checks_is_refused(tmp_path, capsys):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--rounds", "2", TASK)
    assert code == 2 and "--until-checks" in err


def test_a_capped_run_says_what_it_spent_at_least_when_a_cost_is_unknown(
    tmp_path, capsys, monkeypatch
):
    from metabot import office_run

    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    quiet = Fixer("claude", right=1)  # its model has a price, and it reports no usage
    monkeypatch.setattr(office_run, "get_engine", lambda name: quiet)
    code, out, err = _cli(
        capsys, "office", "run", "--dir", str(root), "-q", "--max-cost", "0.0002", TASK
    )
    assert "spent at least $0.00 of $0.0002: the cost of build is unknown" in out, out + err


# ── round 1's rule, for the run's one writer alone ──────────────────────────


class AsksFirst(Fixer):
    """Asks the gate to write the greeting, and writes it only when it may."""

    def __init__(self, name: str = "codex") -> None:
        super().__init__(name)
        self.replies: list[dict] = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{len(self.jobs)}", job.model)
        msg = {"op": "approve", "call_id": "w1", "tool": "fake/Edit", "cls": "write"}
        reply = await approve({**msg, "preview": "write feature.txt"})
        self.replies.append(reply)
        if reply.get("allow"):
            (Path(job.cwd) / "feature.txt").write_text("hello\n", encoding="utf-8")
        yield Final("wrote hello", f"s-{len(self.jobs)}")


async def test_a_writable_desk_before_the_writer_that_changes_no_file_is_not_judged(tmp_path):
    desks = {
        "plan": {"role": "roles/plan.md", "harness": "claude"},
        "build": {"role": "roles/build.md", "harness": "codex"},
    }
    root = _trusted(tmp_path, {"tests": WANT}, desks=desks)
    planner, fixer = DeskEngine("claude"), Fixer("codex", right=1)
    result = await _relay(root, {"claude": planner, "codex": fixer}, checks=True, rounds=3)
    assert result.ok, [(r.label, r.checks, r.stopped, r.error) for r in result.rows]
    assert len(planner.jobs) == 1 and len(fixer.jobs) == 1
    assert next(r for r in result.rows if r.desk == "plan").checks is None


async def test_a_desk_after_fan_copies_that_changes_no_file_is_not_judged(tmp_path):
    desks = {
        "build": {"role": "roles/build.md", "harness": "codex", "fan": 2},
        "review": {"role": "roles/review.md", "harness": "claude"},
    }
    root = _trusted(tmp_path, {"tests": WANT}, desks=desks)
    fixer, reviewer = Fixer("codex", right=1), DeskEngine("claude")
    result = await _relay(root, {"codex": fixer, "claude": reviewer}, checks=True, rounds=3)
    assert len(reviewer.jobs) == 1
    review = next(r for r in result.rows if r.desk == "review")
    assert review.checks is None and review.next_round is None


async def test_a_first_round_whose_write_was_declined_is_not_asked_again(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    asks = AsksFirst()
    result = await _relay(root, {"codex": asks}, checks=True, rounds=3)
    assert [reply["allow"] for reply in asks.replies] == [False]  # the person is asked once
    assert len(asks.jobs) == 1 and result.rows[-1].next_round is None
    [row] = result.rows
    assert not result.ok and row.checks.startswith("fail")  # the checks still judged it
    assert row.stopped == "round 1 had a declined call; the checks still fail"


async def test_round_two_is_told_its_first_round_changed_no_file(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    talker = Talker(right=2)
    result = await _relay(root, {"codex": talker}, checks=True, rounds=3)
    assert result.ok and len(talker.jobs) == 2
    said = talker.jobs[1].prompt
    assert "This is round 2 of 3. Your round before changed no file, and the gate's" in said
    assert "Your work from the round before is on this branch" not in said


# ── a decline ends the loop ─────────────────────────────────────────────────


class WritesAndAsks(AsksFirst):
    """Writes the wrong greeting, then, from its ``asks_from``-th run, asks for one more write."""

    asks_from = 1

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        n = len(self.jobs)
        yield Started(f"s-{n}", job.model)
        (Path(job.cwd) / "feature.txt").write_text(f"wrong {n}\n", encoding="utf-8")
        if n >= self.asks_from:
            msg = {"op": "approve", "call_id": f"w{n}", "tool": "fake/Edit", "cls": "write"}
            self.replies.append(await approve({**msg, "preview": "write notes.txt"}))
        yield Final("wrote wrong", f"s-{n}")


async def test_a_round_with_a_declined_call_is_not_tried_again(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    desk = WritesAndAsks()
    result = await _relay(root, {"codex": desk}, checks=True, rounds=3)
    assert len(desk.jobs) == 1 and [reply["allow"] for reply in desk.replies] == [False]
    [row] = result.rows
    assert not result.ok and row.checks.startswith("fail") and row.next_round is None
    assert row.stopped == "round 1 had a declined call; the checks still fail"
    assert _record(result.office_run_id)["desks"][0]["declined"] == 1


async def test_a_later_round_with_a_declined_call_ends_the_loop_there(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    desk = WritesAndAsks()
    desk.asks_from = 2
    result = await _relay(root, {"codex": desk}, checks=True, rounds=4)
    assert len(desk.jobs) == 2
    assert result.rows[-1].stopped == "round 2 had a declined call; the checks still fail"


class AsksThenStalls(AsksFirst):
    """Asks for a write, which is declined, then stalls until it is stopped."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started("s-a", job.model)
        msg = {"op": "approve", "call_id": "w1", "tool": "fake/Edit", "cls": "write"}
        self.replies.append(await approve({**msg, "preview": "write feature.txt"}))
        self.waiting.set()
        await asyncio.sleep(30)
        yield Final("never", "s-a")


async def test_a_stopped_run_keeps_its_declined_count_in_the_office_record(tmp_path, home):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    asks = AsksThenStalls()
    task = asyncio.ensure_future(_relay(root, {"codex": asks}, checks=True, rounds=3))
    await asyncio.wait_for(asks.waiting.wait(), 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    [path] = (home / "offices").glob("*.json")
    [desk] = json.loads(path.read_text(encoding="utf-8"))["desks"]
    assert desk["error"] == "cancelled" and desk["declined"] == 1
