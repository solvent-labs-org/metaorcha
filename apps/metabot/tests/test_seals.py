"""Seals: one signed receipt over every run of an office run or a ladder, in order.

Each run's own receipt says where it sits. The seal commits to the set, so a
receipt dropped, changed or added shows, and it signs what governed the run:
the office.yaml, the checks, the rounds and the money cap, and it commits to the task
by a salted hash, as a run commits to its prompt."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest
from emerge.record import hash_value

from fake_chat import answer
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.events import Final, Started, Usage
from metabot.gate import Policy
from metabot.office_run import load_record
from metabot.paths import receipt_path, run_path
from metabot.receipts import journal_matches, list_index, verify_file
from metabot.seals import member_problems, receipt_digest
from test_checks import BUILD, _trusted
from test_office_fan import _fan
from test_office_rounds import LOOP, PRICES, WANT, Fixer, Stalls
from test_office_run import TASK, DeskEngine, _relay


def _envelope(run_id: str) -> dict:
    return json.loads(receipt_path(run_id).read_text(encoding="utf-8"))


def _signed(run_id: str) -> dict[str, dict]:
    return {v["check"]: v for v in _envelope(run_id)["verdicts"]}


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


async def _looped(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    result = await _relay(root, {"codex": Fixer()}, checks=True, rounds=3)
    return root, result.office_run_id


# ── an office run ───────────────────────────────────────────────────────────


async def test_an_office_run_is_sealed_over_every_run_in_order(tmp_path):
    root, office = await _looped(tmp_path)
    seal = receipt_path(office)
    assert verify_file(seal).valid and journal_matches(office)
    steps = _envelope(office)["steps"]
    members = ["build", "build.checks", "build.r2", "build.r2.checks"]
    assert [s["call_id"] for s in steps] == [f"{office}.{m}" for m in members]
    assert [s["tool"] for s in steps] == ["metabot.member.desk", "metabot.member.checks"] * 2
    assert [s["success"] for s in steps] == [False, False, True, True]
    signed = _signed(office)
    assert signed["seal"]["result"] == "pass"
    assert signed["seal"]["detail"] == "kind=office;name=shop;mode=relay;members=4"
    spec = hashlib.sha256((root / "office.yaml").read_bytes()).hexdigest()
    assert signed["workflow"]["detail"] == (
        f"office_sha256={spec};checks=on;rounds=3;allow_write=false"
    )
    kept = _journal(office)
    assert kept["meta"]["task"] == TASK
    opening = {"field": "task", "salt": kept["salts"]["task"], "text": TASK}
    assert signed["content"]["detail"] == f"task={hash_value(opening)}"
    assert member_problems(_envelope(office), None) == []
    assert load_record(office)["seal"] == str(seal)
    assert office not in {row["run_id"] for row in list_index()}  # a seal is not a run to count


async def test_a_dropped_changed_or_added_member_receipt_shows(tmp_path):
    _, office = await _looped(tmp_path)
    envelope = _envelope(office)
    dropped = receipt_path(f"{office}.build.checks")
    kept = dropped.read_bytes()
    dropped.unlink()
    assert member_problems(envelope, None) == [f"{office}.build.checks: its receipt is missing"]
    dropped.write_bytes(kept)
    changed = receipt_path(f"{office}.build.r2")
    changed.write_bytes(changed.read_bytes() + b"\n")
    assert member_problems(envelope, None) == [
        f"{office}.build.r2: its receipt is not the one sealed"
    ]
    changed.write_bytes(changed.read_bytes()[:-1])
    receipt_path(f"{office}.extra").write_bytes(kept)
    assert member_problems(envelope, None) == [
        f"{office}.extra: a receipt of this run that the seal does not hold"
    ]


async def test_a_seal_checks_its_members_beside_it_in_a_folder(tmp_path):
    _, office = await _looped(tmp_path)
    folder = tmp_path / "shared"
    folder.mkdir()
    for path in receipt_path(office).parent.glob(f"{office}*.json"):
        (folder / path.name).write_bytes(path.read_bytes())
    for path in receipt_path(office).parent.glob(f"{office}.*.json"):
        path.unlink()  # gone from this machine: the folder holds them
    assert member_problems(_envelope(office), folder) == []
    (folder / f"{office}.build.json").unlink()
    assert member_problems(_envelope(office), folder) == [f"{office}.build: its receipt is missing"]


def test_a_seal_from_elsewhere_never_reaches_outside_the_folder(tmp_path):
    step = {"call_id": "../../etc/passwd", "args_hash": hash_value({"run_id": "../../etc/passwd"})}
    envelope = {"run_id": "mb-1", "steps": [{**step, "output_hash": hash_value("0" * 64)}]}
    assert member_problems(envelope, tmp_path) == ["../../etc/passwd: not a run id"]
    other = {"call_id": "mb-1.build", "args_hash": hash_value({"run_id": "mb-2.build"})}
    envelope = {"run_id": "mb-1", "steps": [{**other, "output_hash": hash_value(None)}]}
    assert member_problems(envelope, tmp_path) == ["mb-1.build: its step does not name it"]


async def test_a_stopped_office_run_is_sealed_as_failed(tmp_path, home):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    stalls = Stalls()
    task = asyncio.ensure_future(_relay(root, {"codex": stalls}, checks=True, rounds=3))
    await asyncio.wait_for(stalls.waiting.wait(), 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    [path] = (home / "offices").glob("*.json")
    office = path.stem
    assert verify_file(receipt_path(office)).valid
    assert [s["call_id"] for s in _envelope(office)["steps"]] == [
        f"{office}.build", f"{office}.build.checks", f"{office}.build.r2",
    ]  # fmt: skip
    assert _signed(office)["seal"]["result"] == "fail"
    assert member_problems(_envelope(office), None) == []


async def test_a_capped_office_run_seals_its_whole_spend(tmp_path):
    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    fixer = Fixer("claude", right=99, usage={"prompt_tokens": 100, "completion_tokens": 0})
    result = await _relay(root, {"claude": fixer}, checks=True, rounds=5, max_cost_musd=200)
    budget = _signed(result.office_run_id)["budget"]
    assert (budget["result"], budget["detail"]) == ("pass", "cap_musd=200;spent_musd=200")


def test_receipts_verify_checks_an_office_runs_members(tmp_path, capsys):
    _, office = asyncio.run(_looped(tmp_path))
    assert main(["receipts", "verify", office]) == 0
    assert "4 members: each present, unchanged, and valid" in capsys.readouterr().out
    receipt_path(f"{office}.build").unlink()
    assert main(["receipts", "verify", office]) == 1
    out = capsys.readouterr().out
    assert out.startswith("INVALID") and f"{office}.build: its receipt is missing" in out


def test_an_edited_task_in_a_seals_journal_does_not_match(tmp_path, capsys):
    _, office = asyncio.run(_looped(tmp_path))
    assert journal_matches(office)
    kept = _journal(office)
    kept["meta"]["task"] = "a task nobody gave"
    (run_path(office) / "journal.json").write_text(json.dumps(kept), encoding="utf-8")
    assert journal_matches(office) is False
    assert main(["receipts", "verify", office]) == 1
    assert "journal DOES NOT MATCH the receipt" in capsys.readouterr().out


def test_receipts_show_says_the_seal_holds(tmp_path, capsys):
    _, office = asyncio.run(_looped(tmp_path))
    assert main(["receipts", "show", office]) == 0
    assert f"seal        {receipt_path(office)} (valid, 4 members)" in capsys.readouterr().out


# ── a ladder ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role, private", [((), "false"), (("--role", "private"), "true")])
def test_a_ladder_is_sealed_over_its_rungs(chat, tmp_path, capsys, role, private):
    fake = chat([answer("four")])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    assert main(["ask", "--ladder", *role, "--json", "--cwd", str(tmp_path), "2+2"]) == 0
    ladder = json.loads(capsys.readouterr().out)["ladder"]["ladder_id"]
    assert verify_file(receipt_path(ladder)).valid and journal_matches(ladder)
    steps = _envelope(ladder)["steps"]
    assert [(s["call_id"], s["tool"]) for s in steps] == [(f"{ladder}.1", "metabot.member.rung")]
    seal = _signed(ladder)["seal"]
    assert (seal["result"], seal["detail"]) == ("pass", "kind=ladder;members=1")
    assert _signed(ladder)["workflow"]["detail"] == f"private={private}"
    opening = {"field": "task", "salt": _journal(ladder)["salts"]["task"], "text": "2+2"}
    assert _signed(ladder)["content"]["detail"] == f"task={hash_value(opening)}"
    sealed = receipt_path(ladder).read_text(encoding="utf-8")
    assert hashlib.sha256(b"2+2").hexdigest() not in sealed and hash_value("2+2") not in sealed
    assert member_problems(_envelope(ladder), None) == []


def test_a_seal_names_a_member_that_never_got_a_receipt(tmp_path):
    from test_ladder import THREE, _fake_runs, _ladder

    run_rung, _ = _fake_runs(tmp_path, {})  # fake runs: their receipts are not in the home
    result = _ladder(THREE, run_rung)
    steps = _envelope(result.ladder_id)["steps"]
    assert [s["output_hash"] for s in steps] == [hash_value(None)]
    assert member_problems(_envelope(result.ladder_id), None) == []
    Path(receipt_path(f"{result.ladder_id}.1")).write_text("{}", encoding="utf-8")
    assert member_problems(_envelope(result.ladder_id), None) == [
        f"{result.ladder_id}.1: the seal says it has no receipt, but one is here"
    ]


def test_office_run_says_where_its_seal_is(tmp_path, capsys, monkeypatch):
    from metabot import office_run
    from test_office_run import _cli

    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    monkeypatch.setattr(office_run, "get_engine", lambda name: Fixer(right=1))
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", "--checks", TASK)
    assert code == 0, err
    office = next(w for w in out.split() if w.startswith("mb-"))
    assert f"  sealed over every run: metabot receipts verify {office}" in out.splitlines()


def test_a_ladder_seal_holds_each_rungs_checks_run(chat, tmp_path, capsys):
    fake = chat([answer("four")])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    argv = ["ask", "--ladder", "--json", "--check", "true", "--cwd", str(tmp_path), "2+2"]
    assert main(argv) == 0
    ladder = json.loads(capsys.readouterr().out)["ladder"]["ladder_id"]
    steps = _envelope(ladder)["steps"]
    assert [(s["call_id"], s["tool"], s["success"]) for s in steps] == [
        (f"{ladder}.1", "metabot.member.rung", True),
        (f"{ladder}.1.checks", "metabot.member.checks", True),
    ]
    assert member_problems(_envelope(ladder), None) == []


def test_a_member_sealed_as_it_was_must_still_verify(tmp_path):
    member = receipt_path("mb-1.build")
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_text("{}", encoding="utf-8")  # bad before it was sealed: the digest matches
    step = {
        "call_id": "mb-1.build",
        "args_hash": hash_value({"run_id": "mb-1.build"}),
        "output_hash": hash_value(receipt_digest(member)),
    }
    envelope = {"run_id": "mb-1", "steps": [step]}
    assert member_problems(envelope, None) == ["mb-1.build: its receipt does not verify"]


def test_the_office_page_says_the_seal_holds(tmp_path):
    from test_web_loops import _looped as _looped_page
    from test_web_office import _start, office_ui
    from test_web_runs import _ended, _until

    root = _looped_page(tmp_path)
    with office_ui(root, {"codex": Fixer()}) as (client, guard):
        office = _start(client, guard, root, "build", until_checks="1", rounds="3")
        _until(client, office, _ended)
        page = client.get(f"/offices/{office}").text
    assert '<th>seal</th><td><span class="ok">valid, 4 members</span></td>' in page


async def test_a_project_that_keeps_receipts_keeps_the_seal_and_checks_alone(tmp_path):
    save_config(Config(project_receipts=True))
    root, office = await _looped(tmp_path)
    kept = root / ".orcha" / "receipts"
    seal = kept / f"{office}.json"
    assert verify_file(seal).valid
    for path in receipt_path(office).parent.glob(f"{office}*.json"):
        path.unlink()  # the home's copies gone: the project's folder must hold it all
    envelope = json.loads(seal.read_text(encoding="utf-8"))
    assert member_problems(envelope, kept) == []


# ── what a stop or a break leaves: the seal holds every run that sealed ─────


class Breaks(Fixer):
    """Spends, then its stream breaks: the run seals what it saw and the error goes up."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started("s-1", job.model)
        yield Usage({"prompt_tokens": 100, "completion_tokens": 0})
        raise RuntimeError("the stream broke")


class Waits(DeskEngine):
    """Starts, then waits until it is stopped."""

    def __init__(self) -> None:
        super().__init__("claude")
        self.waiting = asyncio.Event()

    async def run(self, job, *, board=None, approve):
        yield Started("s-w", job.model)
        self.waiting.set()
        await asyncio.sleep(30)
        yield Final("never", "s-w")


def _full_disk(monkeypatch, ending: str) -> None:
    """The disk fills as a run whose id ends so is sealed, after its engine ran: it leaves
    no receipt."""
    from metabot import runner

    sealed = runner.seal_and_store

    def full(**kw):
        if kw["run_id"].endswith(ending):
            raise OSError(errno.ENOSPC, "No space left on device")
        return sealed(**kw)

    monkeypatch.setattr(runner, "seal_and_store", full)


def _office_of(home) -> str:
    [path] = (home / "offices").glob("*.json")
    return path.stem


async def test_a_stop_while_a_desk_commits_seals_the_receipt_it_left(tmp_path, home, monkeypatch):
    from metabot import office_run

    real, committing = office_run._commit_work, threading.Event()

    def slow(*args, **kwargs):
        committing.set()
        time.sleep(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "_commit_work", slow)
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    task = asyncio.ensure_future(_relay(root, {"codex": Fixer()}, checks=True, rounds=3))
    await asyncio.wait_for(asyncio.to_thread(committing.wait, 20), 25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    office = _office_of(home)
    [step] = _envelope(office)["steps"]
    assert step["output_hash"] == hash_value(receipt_digest(receipt_path(f"{office}.build")))
    assert member_problems(_envelope(office), None) == []
    [desk] = load_record(office)["desks"]  # the record names what the seal holds
    assert desk["receipt"] == str(receipt_path(f"{office}.build"))


async def test_a_desk_whose_engine_breaks_still_counts_in_the_sealed_spend(tmp_path, home):
    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    with pytest.raises(RuntimeError):
        await _relay(root, {"claude": Breaks("claude")}, checks=True, max_cost_musd=50)
    office = _office_of(home)
    budget = _signed(office)["budget"]
    assert (budget["result"], budget["detail"]) == ("fail", "cap_musd=50;spent_musd=100")
    assert member_problems(_envelope(office), None) == []


async def test_a_desk_that_ran_and_could_not_be_sealed_leaves_the_office_cost_unknown(
    tmp_path, home, monkeypatch
):
    save_config(Config(prices=PRICES))
    desks = {
        "build": {"role": "roles/build.md", "harness": "claude", "model": "m-loop"},
        "review": {"role": "roles/review.md", "harness": "codex", "model": "m-loop"},
    }
    root = _trusted(tmp_path, {"tests": "true"}, desks=desks)
    spends = {"prompt_tokens": 100, "completion_tokens": 0}
    build, review = Fixer("claude", right=1, usage=spends), Fixer("codex", right=1, usage=spends)
    _full_disk(monkeypatch, ".build")
    result = await _fan(root, {"claude": build, "codex": review}, max_cost_musd=150)
    assert len(build.jobs) == 1 and not review.jobs  # what build spent is not known
    [built] = [row for row in result.rows if row.desk == "build"]
    assert not result.ok and built.error == "OSError: [Errno 28] No space left on device"
    assert result.cost_unknown == ["build"]
    office = _office_of(home)
    budget = _signed(office)["budget"]
    assert (budget["result"], budget["detail"]) == ("warn", "cap_musd=150;spent_musd=none")
    record = load_record(office)
    assert record["cost_unknown"] == ["build"]
    [stopped] = [desk for desk in record["desks"] if desk["desk"] == "review"]
    assert stopped["stopped"] == "build's cost is unknown, so the office's $0.00015 cannot be kept"


def test_office_run_says_what_it_spent_at_least_when_a_desk_could_not_be_sealed(
    tmp_path, home, capsys, monkeypatch
):
    from metabot import office_run
    from test_office_run import _cli

    save_config(Config(prices=PRICES))
    desks = {
        "build": {"role": "roles/build.md", "harness": "claude", "model": "m-loop"},
        "review": {"role": "roles/review.md", "harness": "codex", "model": "m-loop"},
    }
    root = _trusted(tmp_path, {"tests": "true"}, desks=desks)
    spends = {"prompt_tokens": 100, "completion_tokens": 0}
    engines = {name: Fixer(name, right=1, usage=spends) for name in ("claude", "codex")}
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    _full_disk(monkeypatch, ".build")
    argv = ["office", "run", "--dir", str(root), "--fan", "--max-cost", "0.00015", "-q", TASK]
    code, out, err = _cli(capsys, *argv)
    assert code == 1, err
    lines = out.splitlines()
    [build] = [line for line in lines if line.strip().startswith("build ")]
    assert "No space left on device" in build
    assert "  spent at least $0.00 of $0.00015: the cost of build is unknown" in lines


def test_office_run_on_a_disk_that_stays_full_still_says_what_it_spent(
    tmp_path, home, capsys, monkeypatch
):
    from metabot import office_run
    from test_office_run import _cli

    save_config(Config(prices=PRICES))
    desks = {
        "build": {"role": "roles/build.md", "harness": "claude", "model": "m-loop"},
        "review": {"role": "roles/review.md", "harness": "codex", "model": "m-loop"},
    }
    root = _trusted(tmp_path, {"tests": "true"}, desks=desks)
    spends = {"prompt_tokens": 100, "completion_tokens": 0}
    engines = {name: Fixer(name, right=1, usage=spends) for name in ("claude", "codex")}
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    from metabot import runner

    sealed, wrote, full = runner.seal_and_store, office_run.write_private, []

    def seal(**kw):
        if kw["run_id"].endswith(".build"):
            full.append(kw["run_id"])
        if full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return sealed(**kw)

    def write(*a, **kw):  # once the disk is full, it stays full
        if full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return wrote(*a, **kw)

    monkeypatch.setattr(runner, "seal_and_store", seal)
    monkeypatch.setattr(office_run, "write_private", write)
    argv = ["office", "run", "--dir", str(root), "--fan", "--max-cost", "0.00015", "-q", TASK]
    code, out, err = _cli(capsys, *argv)
    assert code == 1, err
    lines = out.splitlines()
    assert any(line.strip().startswith("build ") for line in lines)
    assert "  spent at least $0.00 of $0.00015: the cost of build is unknown" in lines
    said = err.splitlines()  # as an error, which --quiet keeps
    assert "[metabot] the office's record could not be written: " + FULL in said


FULL = "[Errno 28] No space left on device"


def _desk_engines() -> dict:
    from test_office_run import _build

    return {
        "claude": DeskEngine("claude", act=lambda job: "PLAN: write feature.txt"),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


@pytest.mark.parametrize(("how", "code"), [("error", 1), ("cancel", 130), ("done", 1)])
def test_an_office_run_whose_record_cannot_be_written_says_so_however_it_ends(
    tmp_path, home, capsys, monkeypatch, how, code
):
    from metabot import office_run
    from metabot.desks import DeskGitError
    from metabot.runner import RunCancelled
    from test_office_run import _cli, _repo

    engines = _desk_engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    saved, last_round, seen = office_run._save, office_run._last_round, []

    def save(record, rows):  # the disk fills as the office run closes
        if record.get("finished_at"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return saved(record, rows)

    def stops_at_the_second_desk(run, row):
        seen.append(row.label)
        if len(seen) == 2 and how == "error":
            raise DeskGitError("not a commit here: refs/heads/x")
        if len(seen) == 2 and how == "cancel":
            raise RunCancelled(None)
        return last_round(run, row)

    monkeypatch.setattr(office_run, "_save", save)
    monkeypatch.setattr(office_run, "_last_round", stops_at_the_second_desk)
    ended, out, err = _cli(capsys, "office", "run", "--dir", str(_repo(tmp_path)), "-q", TASK)
    assert ended == code, err  # a run whose record is not on disk did not end well
    assert "[metabot] the office's record could not be written: " + FULL in err.splitlines()
    assert "could not be written" not in out  # said once


def test_an_office_run_whose_seal_cannot_be_signed_says_why(tmp_path, home, capsys, monkeypatch):
    from metabot import office_run
    from test_office_run import _cli, _repo

    engines = _desk_engines()
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])

    def cannot_seal(run):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(office_run, "_seal", cannot_seal)
    code, out, err = _cli(capsys, "office", "run", "--dir", str(_repo(tmp_path)), "-q", TASK)
    assert code == 0, err  # its record says so, and every desk's receipt stands
    assert "[metabot] the office run could not be sealed: " + FULL in err.splitlines()
    assert "sealed over every run" not in out


async def _rungs(tmp_path, engines: dict, **kw):
    """A ladder whose rungs run as ``ask`` runs them, each on its own engine."""
    from metabot.ladder import run_ladder
    from metabot.paths import new_run_id
    from metabot.runner import run_job
    from test_ladder import THREE

    async def run_rung(rung, run_id, meta, cap, where):
        engine = engines[meta["rung"]]
        return await run_job(
            engine=engine, prompt="2+2", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
            model="m-loop", run_id=run_id, meta=meta,
        )  # fmt: skip

    ladder = new_run_id()
    run = run_ladder(THREE, run_rung, cfg=Config(), ladder_id=ladder, gated=lambda e: True, **kw)
    return ladder, asyncio.ensure_future(run)


async def test_a_ladder_stopped_in_a_rung_seals_that_rung(tmp_path):
    waits = Waits()
    ladder, task = await _rungs(tmp_path, {1: DeskEngine("openai", fail="it broke"), 2: waits})
    await asyncio.wait_for(waits.waiting.wait(), 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    steps = _envelope(ladder)["steps"]
    assert [s["call_id"] for s in steps] == [f"{ladder}.1", f"{ladder}.2"]
    assert steps[1]["output_hash"] == hash_value(receipt_digest(receipt_path(f"{ladder}.2")))
    assert _signed(ladder)["seal"]["result"] == "fail"
    assert member_problems(_envelope(ladder), None) == []


async def test_a_ladder_whose_engine_breaks_seals_the_rung_and_its_spend(tmp_path):
    save_config(Config(prices=PRICES))
    ladder, task = await _rungs(tmp_path, {1: Breaks("claude")}, max_cost_musd=50)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 20)
    assert [s["call_id"] for s in _envelope(ladder)["steps"]] == [f"{ladder}.1"]
    budget = _signed(ladder)["budget"]
    assert (budget["result"], budget["detail"]) == ("fail", "cap_musd=50;spent_musd=100")
    assert member_problems(_envelope(ladder), None) == []


async def test_a_rung_that_ran_and_could_not_be_sealed_leaves_the_ladder_cost_unknown(
    tmp_path, monkeypatch
):
    save_config(Config(prices=PRICES))
    _full_disk(monkeypatch, ".1")
    spends = Fixer("claude", right=1, usage={"prompt_tokens": 100, "completion_tokens": 0})
    ladder, task = await _rungs(tmp_path, {1: spends}, max_cost_musd=500)
    with pytest.raises(OSError, match="No space left"):
        await asyncio.wait_for(task, 20)
    assert len(spends.jobs) == 1
    budget = _signed(ladder)["budget"]
    assert (budget["result"], budget["detail"]) == ("warn", "cap_musd=500;spent_musd=none")


def test_receipts_verify_says_which_members_were_sealed_with_no_receipt(tmp_path, capsys):
    from test_ladder import THREE, _fake_runs, _ladder

    run_rung, _ = _fake_runs(tmp_path, {})
    result = _ladder(THREE, run_rung)
    assert main(["receipts", "verify", result.ladder_id]) == 0
    out = capsys.readouterr().out
    assert "each present" not in out
    assert "1 members: 0 present, unchanged, and valid; 1 sealed with no receipt" in out
    assert main(["receipts", "show", result.ladder_id]) == 0
    assert "seal      valid, 1 members, 1 with no receipt" in capsys.readouterr().out.splitlines()
    receipt_path(f"{result.ladder_id}.9").write_text("{}", encoding="utf-8")
    assert main(["receipts", "verify", result.ladder_id]) == 1  # and so it says when one fails
    assert "  1 sealed with no receipt" in capsys.readouterr().out.splitlines()


def test_a_seal_is_judged_as_this_runs_seal_and_never_by_the_records_path(tmp_path, home, capsys):
    from metabot.seals import seal_state

    _, office = asyncio.run(_looped(tmp_path))
    seal = receipt_path(office)
    assert seal_state(seal, "mb-20260101T000000Z-000000") == "INVALID: not this run's seal"
    kept = home / "offices" / f"{office}.json"
    record = json.loads(kept.read_text(encoding="utf-8"))
    record["seal"] = str(receipt_path(f"{office}.build"))  # hand-edited: a run that is no seal
    kept.write_text(json.dumps(record), encoding="utf-8")
    assert main(["receipts", "show", office]) == 0
    assert f"seal        {seal} (valid, 4 members)" in capsys.readouterr().out
    from metabot.web_data import office_view

    assert office_view(office)["seal_state"] == "valid, 4 members"


def test_verifying_a_seal_from_elsewhere_leaves_no_home_behind(tmp_path, monkeypatch, capsys):
    _, office = asyncio.run(_looped(tmp_path))
    folder = tmp_path / "shared"
    folder.mkdir()
    for path in receipt_path(office).parent.glob(f"{office}*.json"):
        (folder / path.name).write_bytes(path.read_bytes())
    elsewhere = tmp_path / "another-machine"
    monkeypatch.setenv("METABOT_HOME", str(elsewhere))
    assert main(["receipts", "verify", str(folder / f"{office}.json")]) == 0
    assert "4 members: each present, unchanged, and valid" in capsys.readouterr().out
    assert not elsewhere.exists()


def test_a_member_listed_twice_is_a_problem_and_read_once(tmp_path):
    step = {
        "call_id": "mb-1.build",
        "args_hash": hash_value({"run_id": "mb-1.build"}),
        "output_hash": hash_value(None),
    }
    envelope = {"run_id": "mb-1", "steps": [step, step, step]}
    assert member_problems(envelope, tmp_path) == [
        "mb-1.build: the seal lists it twice",
        "mb-1.build: the seal lists it twice",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_member_that_cannot_be_read_is_a_problem_not_a_crash(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    member = folder / "mb-1.build.json"
    member.write_text("{}", encoding="utf-8")
    step = {
        "call_id": "mb-1.build",
        "args_hash": hash_value({"run_id": "mb-1.build"}),
        "output_hash": hash_value("0" * 64),
    }
    member.chmod(0)
    try:
        problems = member_problems({"run_id": "mb-1", "steps": [step]}, folder)
    finally:
        member.chmod(0o600)
    assert problems == ["mb-1.build: its receipt cannot be read"]


def test_the_run_page_of_a_seal_says_whether_its_members_hold(tmp_path):
    from test_web_runs import ui

    _, office = asyncio.run(_looped(tmp_path))
    with ui(tmp_path) as (client, _guard):
        page = client.get(f"/runs/{office}").text
        assert '<span class="ok">valid, 4 members</span>' in page
        receipt_path(f"{office}.build").unlink()
        page = client.get(f"/runs/{office}").text
    assert '<span class="bad">1 problem(s) with its 4 members</span>' in page


async def test_an_error_after_a_desks_checks_sealed_still_seals_them(tmp_path, home, monkeypatch):
    from metabot import office_run

    real = office_run.run_gate

    async def breaks(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("after the checks sealed")

    monkeypatch.setattr(office_run, "run_gate", breaks)
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    with pytest.raises(RuntimeError):
        await _relay(root, {"codex": Fixer()}, checks=True, rounds=3)
    office = _office_of(home)
    steps = _envelope(office)["steps"]
    assert [s["call_id"] for s in steps] == [f"{office}.build", f"{office}.build.checks"]
    assert member_problems(_envelope(office), None) == []


async def test_an_error_after_a_rungs_checks_sealed_still_seals_them(tmp_path, monkeypatch):
    from metabot import ladder as ladders

    real = ladders.run_gate

    async def breaks(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("after the checks sealed")

    monkeypatch.setattr(ladders, "run_gate", breaks)
    engines = {1: DeskEngine("openai")}
    ladder, task = await _rungs(tmp_path, engines, checks={"ask:1": "true"}, cwd=tmp_path)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 20)
    steps = _envelope(ladder)["steps"]
    assert [s["call_id"] for s in steps] == [f"{ladder}.1", f"{ladder}.1.checks"]
    assert member_problems(_envelope(ladder), None) == []


async def test_an_office_run_stopped_twice_while_it_opens_is_still_sealed(
    tmp_path, home, monkeypatch
):
    from metabot import office_run

    real, opening = office_run._open, threading.Event()

    def slow(*args, **kwargs):
        opening.set()
        time.sleep(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "_open", slow)
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    task = asyncio.ensure_future(_relay(root, {"codex": Fixer()}, checks=True, rounds=3))
    await asyncio.wait_for(asyncio.to_thread(opening.wait, 20), 25)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()  # Stop again, or Stop and then quit the page, before the run has opened
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    for _ in range(100):  # the open finishes in its thread: its record is what it left
        if list((home / "offices").glob("*.json")):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    office = _office_of(home)
    record = load_record(office)
    assert record["finished_at"] and record["seal"] == str(receipt_path(office))
    assert verify_file(receipt_path(office)).valid
    assert _signed(office)["seal"]["result"] == "fail"


def test_an_office_run_quit_twice_while_it_opens_is_still_sealed(tmp_path, home, monkeypatch):
    from metabot import office_run

    real, opening = office_run._open, threading.Event()

    def slow(*args, **kwargs):
        opening.set()
        time.sleep(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "_open", slow)
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)

    async def quit_twice():
        task = asyncio.ensure_future(_relay(root, {"codex": Fixer()}, checks=True, rounds=3))
        await asyncio.to_thread(opening.wait, 20)
        task.cancel()  # Ctrl-C: the run is stopped
        await asyncio.sleep(0.1)
        raise KeyboardInterrupt  # Ctrl-C again: asyncio.run stops and cancels every task

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(quit_twice())
    record = load_record(_office_of(home))
    assert record["finished_at"] and record["seal"] == str(receipt_path(record["office_run_id"]))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any folder")
def test_a_home_that_cannot_be_read_is_a_problem_not_a_crash(tmp_path, monkeypatch, capsys):
    _, office = asyncio.run(_looped(tmp_path))
    folder = tmp_path / "shared"
    folder.mkdir()
    for path in receipt_path(office).parent.glob(f"{office}*.json"):
        (folder / path.name).write_bytes(path.read_bytes())
    locked = tmp_path / "locked"
    (locked / "home" / "receipts").mkdir(parents=True)
    monkeypatch.setenv("METABOT_HOME", str(locked / "home"))
    locked.chmod(0)
    try:
        code = main(["receipts", "verify", str(folder / f"{office}.json")])
    finally:
        locked.chmod(0o700)
    assert code == 1
    assert f"    {locked / 'home' / 'receipts'}: cannot be read" in capsys.readouterr().out


# ── what show and verify read: only what MetaBot keeps, and never a crash ────


def test_receipts_show_judges_desk_receipts_where_they_are_kept(tmp_path, home, capsys):
    root, office = asyncio.run(_looped(tmp_path))
    kept = home / "offices" / f"{office}.json"
    record = json.loads(kept.read_text(encoding="utf-8"))
    for row in record["desks"]:  # hand-edited: paths to a file that is no receipt
        row["receipt"] = row["checks_receipt"] = str(root / "office.yaml")
    kept.write_text(json.dumps(record), encoding="utf-8")
    assert main(["receipts", "show", office]) == 0
    out = capsys.readouterr().out
    assert "INVALID" not in out
    assert out.count("(receipt valid)") == 2


def test_a_receipt_nested_too_deep_is_invalid_not_a_crash(tmp_path, capsys):
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    assert main(["receipts", "verify", str(deep)]) == 1
    assert capsys.readouterr().out.startswith("INVALID")
    _, office = asyncio.run(_looped(tmp_path))
    journal = run_path(f"{office}.build") / "journal.json"
    journal.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    assert journal_matches(f"{office}.build") is False


def test_receipts_show_leaves_no_home_behind(tmp_path, monkeypatch, capsys):
    fresh = tmp_path / "fresh-home"
    monkeypatch.setenv("METABOT_HOME", str(fresh))
    assert main(["receipts", "show", "mb-20260101T000000Z-000000"]) == 2
    from metabot.ladder import load_ladder

    assert load_ladder("mb-20260101T000000Z-000000") is None
    assert not fresh.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists any folder")
def test_a_seal_folder_that_cannot_be_listed_is_a_problem(tmp_path):
    _, office = asyncio.run(_looped(tmp_path))
    folder = tmp_path / "shared"
    folder.mkdir()
    for path in receipt_path(office).parent.glob(f"{office}*.json"):
        (folder / path.name).write_bytes(path.read_bytes())
        path.unlink()  # gone from the home: only the folder holds the run
    envelope = json.loads((folder / f"{office}.json").read_text(encoding="utf-8"))
    folder.chmod(0o300)  # its files can be opened by name, but it cannot be listed
    try:
        problems = member_problems(envelope, folder)
    finally:
        folder.chmod(0o700)
    assert problems == [f"{folder}: cannot be read"]


def test_a_project_that_keeps_receipts_keeps_a_ladders_checks_and_seal(chat, tmp_path, capsys):
    fake = chat([answer("four")])
    cfg = Config(
        openai_base_url=fake.url,
        ladder=[{"engine": "openai", "model": "tiny"}],
        project_receipts=True,
    )
    save_config(cfg)
    argv = ["ask", "--ladder", "--json", "--check", "true", "--cwd", str(tmp_path), "2+2"]
    assert main(argv) == 0
    ladder = json.loads(capsys.readouterr().out)["ladder"]["ladder_id"]
    kept = tmp_path / ".orcha" / "receipts"
    assert sorted(p.name for p in kept.iterdir()) == [
        f"{ladder}.1.checks.json", f"{ladder}.1.json", f"{ladder}.json",
    ]  # fmt: skip
    for path in receipt_path(ladder).parent.glob(f"{ladder}*.json"):
        path.unlink()  # the home's copies gone: the project's folder must hold it all
    envelope = json.loads((kept / f"{ladder}.json").read_text(encoding="utf-8"))
    assert member_problems(envelope, kept) == []


# ── a money cap that a run's unknown cost leaves unkept ─────────────────────


async def test_a_capped_office_run_stops_at_a_run_whose_cost_is_unknown(tmp_path):
    save_config(Config(prices=PRICES))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    quiet = Fixer("claude", right=99)  # its model has a price, and it reports no usage
    result = await _relay(root, {"claude": quiet}, checks=True, rounds=5, max_cost_musd=200)
    assert not result.ok and len(quiet.jobs) == 1
    assert result.rows[-1].cost_musd is None
    assert result.rows[-1].stopped == (
        "build reported no usage MetaBot could read, so the office's $0.0002 cannot be kept"
    )
    budget = _signed(result.office_run_id)["budget"]
    assert (budget["result"], budget["detail"]) == ("warn", "cap_musd=200;spent_musd=none")
    assert load_record(result.office_run_id)["cost_unknown"] == ["build"]


class Unpriced(Fixer):
    """Names, as it starts, a model with no price, and reports what it used."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started("s-u", "claude-unpriced-9")
        yield Usage({"input_tokens": 100, "output_tokens": 50})
        (Path(job.cwd) / "feature.txt").write_text("wrong\n", encoding="utf-8")
        yield Final("wrote wrong", "s-u")


async def test_a_capped_office_run_says_a_run_s_tokens_had_no_price(tmp_path):
    save_config(Config(prices=PRICES))
    desks = {"build": {"role": "roles/build.md", "harness": "claude"}}  # its model, as it starts
    root = _trusted(tmp_path, {"tests": WANT}, desks=desks)
    unpriced = Unpriced("claude")
    result = await _relay(root, {"claude": unpriced}, checks=True, rounds=5, max_cost_musd=200)
    assert len(unpriced.jobs) == 1 and result.rows[-1].cost_musd is None
    assert result.rows[-1].stopped == (
        "build's 150 tokens have no price MetaBot knows, so the office's $0.0002 cannot be kept"
    )


class NamedLate(Fixer):
    """Reports what it used before it names, as it starts, a model priced otherwise."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Usage({"input_tokens": 100, "output_tokens": 50})
        yield Started("s-l", "m-dear")
        (Path(job.cwd) / "feature.txt").write_text("wrong\n", encoding="utf-8")
        yield Final("wrote wrong", "s-l")


async def test_a_capped_office_run_says_a_run_s_tokens_came_before_its_model_was_named(tmp_path):
    save_config(Config(prices={**PRICES, "m-dear": {"in": 5.0, "out": 10.0}}))
    root = _trusted(tmp_path, {"tests": WANT}, desks=LOOP)
    late = NamedLate("claude")
    result = await _relay(root, {"claude": late}, checks=True, rounds=5, max_cost_musd=200)
    # before: its 150 tokens were priced as the model asked for, $0.0002, and the office spent
    assert len(late.jobs) == 1 and result.rows[-1].cost_musd is None
    assert result.rows[-1].stopped == (
        "build's engine reported tokens before it named its model, so the office's $0.0002 "
        "cannot be kept"
    )


async def test_the_next_desk_of_a_capped_relay_does_not_start_after_an_unknown_cost(tmp_path):
    save_config(Config(prices=PRICES))
    desks = {
        "build": {"role": "roles/build.md", "harness": "claude", "model": "m-loop"},
        "review": {"role": "roles/review.md", "harness": "codex", "model": "m-loop"},
    }
    root = _trusted(tmp_path, {"tests": "true"}, desks=desks)
    quiet, reviewer = Fixer("claude", right=1), Fixer("codex", right=1)
    result = await _relay(root, {"claude": quiet, "codex": reviewer}, max_cost_musd=200)
    assert len(quiet.jobs) == 1 and not reviewer.jobs
    review = next(row for row in result.rows if row.desk == "review")
    assert review.stopped == (
        "build reported no usage MetaBot could read, so the office's $0.0002 cannot be kept"
    )


async def test_a_capped_ladder_stops_at_a_rung_whose_cost_is_unknown(tmp_path):
    save_config(Config(prices=PRICES))
    second = Fixer("claude", usage={"prompt_tokens": 100, "completion_tokens": 0})
    ladder, task = await _rungs(
        tmp_path, {1: DeskEngine("claude", fail="it broke"), 2: second}, max_cost_musd=500
    )
    result = await task
    assert not second.jobs
    assert result.stopped == (
        "rung 1 reported no usage MetaBot could read, so the ladder's $0.0005 cannot be kept"
    )
    budget = _signed(ladder)["budget"]
    assert (budget["result"], budget["detail"]) == ("warn", "cap_musd=500;spent_musd=none")
