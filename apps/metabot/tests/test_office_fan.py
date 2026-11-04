"""Fan: one task to several desks at once; copies of one desk inside a relay."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from fake_tty import FakeTty
from metabot import office_run
from metabot.approvals import DenyAll, TtyApprover
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.desks import desk_label
from metabot.events import Final, Started
from metabot.office import OfficeError, load_office
from metabot.office_run import office_journal_path, run_fan, run_relay
from metabot.receipts import journal_matches, list_index, verify_file
from test_office_run import DESKS, TASK, DeskEngine, _build, _build_and_commit, _git, _repo

MEET_S = 10.0  # how long a run waits for the others it should meet


class Busy:
    """How many fake desk runs are going at once, and the most there ever were.

    With ``meet``, each run waits until that many are going, for at most
    ``MEET_S``, and only then takes its delay. A test that runs desks at once
    then holds however slowly the machine makes their worktrees, and a cap
    that lets fewer run fails it. The delay after meeting gives a missing cap
    the time to start one run too many.
    """

    def __init__(self, meet: int = 0) -> None:
        self.now = 0
        self.most = 0
        self.meet = meet
        self.met = asyncio.Event()


class SlowEngine(DeskEngine):
    """A desk engine that takes a moment, so runs overlap when they can."""

    def __init__(self, name: str, busy: Busy, delay: float = 0.05, **kw) -> None:
        super().__init__(name, **kw)
        self.busy = busy
        self.delay = delay

    async def run(self, job, *, board=None, approve):
        self.busy.now += 1
        self.busy.most = max(self.busy.most, self.busy.now)
        if self.busy.meet and self.busy.now >= self.busy.meet:
            self.busy.met.set()
        try:
            if self.busy.meet:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.busy.met.wait(), MEET_S)
            await asyncio.sleep(self.delay)
            async for event in super().run(job, board=board, approve=approve):
                yield event
        finally:
            self.busy.now -= 1


class FailsOn(DeskEngine):
    """Fails in the worktree named ``label``, and works everywhere else."""

    def __init__(self, name: str, label: str, **kw) -> None:
        super().__init__(name, **kw)
        self.label = label

    async def run(self, job, *, board=None, approve):
        self.fail = "codex exited 1" if Path(job.cwd).name == self.label else None
        async for event in super().run(job, board=board, approve=approve):
            yield event


class AskingEngine(DeskEngine):
    """Asks the gate about one write before it answers."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.replies: list[dict] = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        msg = {"op": "approve", "call_id": "w1", "tool": "fake/Edit", "cls": "write"}
        self.replies.append(await approve({**msg, "preview": "edit app.txt"}))
        yield Final("asked", f"s-{self.name}")


def _engines(busy: Busy | None = None) -> dict:
    busy = busy or Busy()
    return {
        "claude": SlowEngine("claude", busy, act=lambda job: "PLAN: write feature.txt"),
        "codex": SlowEngine("codex", busy, act=_build),
        "cursor": SlowEngine("cursor", busy, act=lambda job: "REVIEW: fine"),
    }


async def _fan(root: Path, engines: dict, **kw):
    return await run_fan(
        load_office(root, kw.pop("cap", 3)),
        kw.pop("task", TASK),
        approver=kw.pop("approver", DenyAll()),
        make_engine=lambda name: engines[name],
        **kw,
    )


def _labels(result) -> list[str]:
    return [desk_label(row.desk, row.copy) for row in result.rows]


async def test_fan_gives_each_desk_its_own_worktree_from_one_base(tmp_path, home):
    engines = _engines()
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD").strip()
    result = await _fan(root, engines)
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    assert result.mode == "fan" and _labels(result) == ["plan", "build", "review"]
    office_id = result.office_run_id
    for row, engine in zip(result.rows, ("claude", "codex", "cursor"), strict=True):
        [job] = engines[engine].jobs
        assert Path(job.cwd) == (home / "desks" / office_id / row.desk).resolve()
        assert ".metabot" not in {p.name for p in Path(job.cwd).iterdir()}  # no handoff in a fan
        start = f"{row.branch}~1" if row.commit else row.branch
        assert _git(root, "rev-parse", start).strip() == base
    review_wt = Path(engines["cursor"].jobs[0].cwd)
    assert not (review_wt / "feature.txt").exists()  # the build desk's work is its own
    rows = {r["run_id"]: r for r in list_index() if r.get("office") == office_id}
    assert all(rows[r.run_id]["parent"] is None for r in result.rows)
    record = json.loads(office_journal_path(office_id).read_text(encoding="utf-8"))
    assert record["mode"] == "fan" and record["base"] == base
    assert _git(root, "status", "--porcelain") == ""


async def test_desks_in_a_fan_run_at_once(tmp_path):
    busy = Busy(meet=3)
    engines = _engines(busy)
    result = await _fan(_repo(tmp_path), engines)
    assert result.ok and busy.most == 3


def test_fan_table_has_stat_checks_and_receipt_per_desk(tmp_path, capsys, monkeypatch):
    engines = _engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    root = _repo(tmp_path)
    assert main(["office", "run", "--fan", "--dir", str(root), "-q", TASK]) == 0
    out = capsys.readouterr().out
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    assert f"office run {office_id}  fan" in out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
    assert set(lines) >= {"plan", "build", "review"}
    assert "1 file +1 -0" in lines["build"] and "0 files +0 -0" in lines["review"]
    assert all(
        "ok" in lines[d] and "receipt valid" in lines[d] for d in ("plan", "build", "review")
    )
    for desk in ("plan", "build", "review"):
        assert f"answer of {desk}:" in out
    picks = [line for line in out.splitlines() if line.startswith("pick one:")]
    # the others changed nothing, so only the build desk has work to pick
    assert picks == [f"pick one: metabot desks pick {office_id}/build"]


async def test_copies_are_capped_and_named(tmp_path, home):
    save_config(Config(fan_cap=2))
    desks = {
        "build": {"role": "roles/build.md", "harness": "codex", "fan": 2},
        "alt": {"role": "roles/alt.md", "harness": "claude", "fan": 2},
    }
    busy = Busy(meet=2)
    engines = _engines(busy)
    for engine in engines.values():  # time for a missing cap to start a third copy
        engine.delay = 0.3
    root = _repo(tmp_path / "a", desks)
    result = await _fan(root, engines, cap=2)
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    office_id = result.office_run_id
    assert _labels(result) == ["build.1", "build.2", "alt.1", "alt.2"]
    assert [r.run_id for r in result.rows] == [f"{office_id}.{x}" for x in _labels(result)]
    assert [r.branch for r in result.rows] == [f"metabot/{office_id}/{x}" for x in _labels(result)]
    assert busy.most == 2  # copies run at once, never more than the cap
    assert len(engines["codex"].jobs) == 2 and len(engines["claude"].jobs) == 2
    copy_prompt = engines["codex"].jobs[0].prompt
    assert "one of 2 copies of the build desk" in copy_prompt
    over = {"build": {"role": "roles/build.md", "harness": "codex", "fan": 3}}
    with pytest.raises(OfficeError, match="over this machine's cap of 2"):
        await _fan(_repo(tmp_path / "b", over), engines, cap=2)


async def test_next_desk_gets_every_copy(tmp_path, home):
    engines = _engines()
    desks = {**DESKS, "build": {**DESKS["build"], "fan": 2}}
    root = _repo(tmp_path, desks)
    result = await run_relay(
        load_office(root),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    office_id = result.office_run_id
    assert _labels(result) == ["plan", "build.1", "build.2", "review"]
    [review] = engines["cursor"].jobs
    inbox = Path(review.cwd) / ".metabot" / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["build.1.md", "build.2.md", "plan.md"]
    assert (inbox / "build.2.md").read_text(encoding="utf-8") == "BUILT feature.txt"
    for n in (1, 2):
        assert f"metabot/{office_id}/build.{n}" in review.prompt
    assert not (Path(review.cwd) / "feature.txt").exists()  # it starts where the copies started
    plan_branch = result.rows[0].branch
    for row in result.rows[1:3]:  # both copies start from the plan desk's branch
        assert (
            _git(root, "rev-parse", f"{row.branch}~1").strip()
            == _git(root, "rev-parse", plan_branch).strip()
        )
    rows = {r["run_id"]: r for r in list_index() if r.get("office") == office_id}
    assert [rows[r.run_id]["parent"] for r in result.rows] == [
        None,
        f"{office_id}.plan",
        f"{office_id}.plan",
        f"{office_id}.build.1,{office_id}.build.2",
    ]
    for row in result.rows:
        assert verify_file(row.receipt).valid and journal_matches(row.run_id)


async def test_a_failed_copy_is_kept_and_the_relay_goes_on(tmp_path):
    engines = _engines()
    engines["codex"] = FailsOn("codex", "build.2", act=_build)
    desks = {**DESKS, "build": {**DESKS["build"], "fan": 2}}
    result = await run_relay(
        load_office(_repo(tmp_path, desks)),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    assert _labels(result) == ["plan", "build.1", "build.2", "review"]
    assert [r.ok for r in result.rows] == [True, True, False, True]
    assert result.rows[2].error == "codex exited 1" and result.rows[2].receipt
    assert not result.ok  # one copy failed, and the office run says so
    [review] = engines["cursor"].jobs
    inbox = Path(review.cwd) / ".metabot" / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["build.1.md", "plan.md"]
    assert "build.2" not in review.prompt


async def _copies_of(tmp_path, act) -> tuple:
    engines = _engines()
    engines["codex"] = DeskEngine("codex", act=act)
    desks = {**DESKS, "build": {**DESKS["build"], "fan": 2}}
    result = await run_relay(
        load_office(_repo(tmp_path, desks)),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    [review] = engines["cursor"].jobs
    return result, review


async def test_a_copy_that_changed_no_file_is_not_offered_to_pick(tmp_path):
    def first_builds(job):
        return _build(job) if Path(job.cwd).name == "build.1" else "nothing to do"

    result, review = await _copies_of(tmp_path, first_builds)
    assert [r.ok for r in result.rows] == [True, True, True, True]
    build1, build2 = result.rows[1:3]
    assert build1.commit and build2.commit is None
    assert str(build1.branch) in review.prompt and str(build2.branch) not in review.prompt
    inbox = Path(review.cwd) / ".metabot" / "inbox"
    assert (inbox / "build.2.md").read_text(encoding="utf-8") == "nothing to do"


async def test_a_copy_that_committed_its_own_work_is_handed_on(tmp_path):
    def first_commits(job):
        return _build_and_commit(job) if Path(job.cwd).name == "build.1" else "nothing to do"

    result, review = await _copies_of(tmp_path, first_commits)
    build1, build2 = result.rows[1:3]
    tip = _git(tmp_path / "repo", "rev-parse", f"refs/heads/{build1.branch}").strip()
    assert build1.commit == tip and build2.commit is None
    assert str(build1.branch) in review.prompt and str(build2.branch) not in review.prompt


async def test_copies_that_changed_nothing_offer_nothing_to_pick(tmp_path):
    result, review = await _copies_of(tmp_path, lambda job: "nothing to do")
    assert all(r.ok for r in result.rows)
    assert "ran as copies" not in review.prompt


class WaitsForTheFirst(DeskEngine):
    """Copy 1 ends only once copy 2 runs; copy 2 waits to see copy 1's end on disk."""

    def __init__(self, name: str, home: Path) -> None:
        super().__init__(name, act=_build)
        self.home = home
        self.second_runs = asyncio.Event()
        self.seen: dict | None = None

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        if Path(job.cwd).name == "build.1":
            await asyncio.wait_for(self.second_runs.wait(), 10)
        else:
            self.second_runs.set()
            for _ in range(250):
                [path] = (self.home / "offices").glob("*.json")
                rows = json.loads(path.read_text(encoding="utf-8"))["desks"]
                first = next(row for row in rows if row["label"] == "build.1")
                if first["receipt"]:
                    self.seen = first
                    break
                await asyncio.sleep(0.02)
        yield Final(_build(job), f"s-{self.name}")


async def test_a_copy_that_finished_is_saved_while_another_still_runs(tmp_path, home):
    engines = {"codex": WaitsForTheFirst("codex", home)}
    desks = {"build": {"role": "roles/build.md", "harness": "codex", "fan": 2}}
    result = await run_relay(
        load_office(_repo(tmp_path, desks)),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    seen = engines["codex"].seen
    assert seen is not None and seen["ok"] and seen["commit"] == result.rows[0].commit


async def test_a_desk_with_no_good_copy_stops_the_relay(tmp_path):
    engines = _engines()
    engines["codex"].fail = "codex exited 1"
    desks = {**DESKS, "build": {**DESKS["build"], "fan": 2}}
    result = await run_relay(
        load_office(_repo(tmp_path, desks)),
        TASK,
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
    )
    assert _labels(result) == ["plan", "build.1", "build.2"]
    assert engines["cursor"].jobs == []


async def test_two_prompts_never_interleave(tmp_path):
    tty = FakeTty()
    try:
        engine = AskingEngine("codex")
        desks = {"build": {"role": "roles/build.md", "harness": "codex", "fan": 2}}
        root = _repo(tmp_path, desks)
        task = asyncio.create_task(_fan(root, {"codex": engine}, approver=TtyApprover(tty.path)))
        await tty.wait_for("allow?")
        await asyncio.sleep(0.2)  # time enough for the other copy to ask, if it could
        first = tty.screen()
        assert first.count("allow?") == 1
        asker = "build.1" if "build.1: write call" in first else "build.2"
        assert f"[metabot] {asker}: write call wants to run" in first
        tty.type("y\n")
        other = "build.2" if asker == "build.1" else "build.1"
        await tty.wait_for(f"[metabot] {other}: write call wants to run")
        assert tty.screen().count("allow?") == 2
        tty.type("n\n")
        result = await asyncio.wait_for(task, 10)
    finally:
        tty.close()
    assert sorted(r["allow"] for r in engine.replies) == [False, True]
    assert all(row.ok for row in result.rows)


class Blocked(DeskEngine):
    """Starts, then waits until it is cancelled."""

    def __init__(self, name: str, started: list) -> None:
        super().__init__(name)
        self.started = started

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        self.started.append(Path(job.cwd).name)
        await asyncio.Event().wait()
        yield Final("never", f"s-{self.name}")


async def test_a_cancelled_fan_seals_what_ran_and_names_what_never_began(tmp_path):
    save_config(Config(fan_cap=2))
    started: list = []
    desks = {
        "a": {"role": "roles/a.md", "harness": "codex", "fan": 2},
        "b": {"role": "roles/b.md", "harness": "claude"},
    }
    engines = {"codex": Blocked("codex", started), "claude": Blocked("claude", started)}
    task = asyncio.create_task(_fan(_repo(tmp_path, desks), engines, cap=2))
    for _ in range(500):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert sorted(started) == ["a.1", "a.2"]  # the cap holds b back
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [office_id] = [p.stem for p in (Path(office_journal_path("x")).parent).glob("*.json")]
    record = json.loads(office_journal_path(office_id).read_text(encoding="utf-8"))
    rows = {d["label"]: d for d in record["desks"]}
    assert set(rows) == {"a.1", "a.2", "b"} and record["finished_at"]
    for label in ("a.1", "a.2"):
        assert rows[label]["error"] == "cancelled"
        assert verify_file(rows[label]["receipt"]).valid
    assert rows["b"]["error"] == "cancelled before it began" and rows["b"]["receipt"] is None
    assert engines["claude"].jobs == []


def test_desks_at_once_name_themselves_in_the_trace_and_the_record(tmp_path, capsys, monkeypatch):
    engines = _engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    desks = {**DESKS, "review": {**DESKS["review"], "fan": 2}}
    root = _repo(tmp_path, desks)
    assert main(["office", "run", "--dir", str(root), TASK]) == 0
    out, err = capsys.readouterr()
    assert "review.1| [metabot] started" in err and "review.2| [metabot] started" in err
    assert "plan| " not in err  # a desk running alone needs no name on its lines
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    assert "answer of review.1:" in out and "answer of review.2:" in out
    assert f"pick one: metabot desks pick {office_id}/review.1" in out
    assert f"pick one: metabot desks pick {office_id}/review.2" in out
    assert "next: metabot desks pick" not in out
    assert main(["receipts", "show", office_id]) == 0
    shown = [
        line.split()[0] for line in capsys.readouterr().out.splitlines() if line.startswith("  ")
    ]
    assert shown == ["plan", "build", "review.1", "review.2"]


class Crashes(DeskEngine):
    """An engine adapter with a bug: it raises mid-run."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        raise RuntimeError("adapter bug")


async def test_a_crashed_desk_lets_the_others_finish_first(tmp_path):
    engines = {
        "codex": Crashes("codex"),
        "claude": SlowEngine("claude", Busy(), delay=0.3, act=lambda job: "b done"),
    }
    desks = {
        "a": {"role": "roles/a.md", "harness": "codex"},
        "b": {"role": "roles/b.md", "harness": "claude"},
    }
    with pytest.raises(RuntimeError, match="adapter bug"):
        await _fan(_repo(tmp_path, desks), engines)
    [path] = Path(office_journal_path("x")).parent.glob("*.json")
    rows = {d["label"]: d for d in json.loads(path.read_text(encoding="utf-8"))["desks"]}
    assert rows["b"]["ok"] and verify_file(rows["b"]["receipt"]).valid  # b ended first
    assert rows["a"]["error"] == "RuntimeError: adapter bug"
    assert verify_file(rows["a"]["receipt"]).valid  # what the broken run saw is sealed
