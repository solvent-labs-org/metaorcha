"""A routine that fires an approved workflow on a schedule, under the ceiling you approved.

Each firing hashes the workflow's bytes again, needs an approval in force for that exact
sha256, and is refused past its ceiling: firings used up, expired, or this month's money at
or over the cap, the money read from the signed receipts of its earlier firings. A firing
that runs is one seal whose ``workflow_firing`` verdict signs its lineage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fake_chat import text_reply
from metabot import workflow_firing
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.paths import kept_receipts, metabot_home
from metabot.receipts import ensure_key, seal_and_store, verify_file
from metabot.routines import Firing, _append, fire, firings, get_routine, routines
from metabot.serve import Scheduler
from metabot.workflow_approval import approval_state, approve, revoke
from metabot.workflow_firing import (
    FIRING_CHECK,
    NOT_APPROVED,
    SEAL_PREFIX,
    ceiling_state,
    save_workflow_routine,
)
from metabot.workflows import FORMAT, Workflow, load_workflow, store_workflow

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DOLLAR = {"prompt_tokens": 1_000_000, "completion_tokens": 0}  # $1.00 at the price below
PRICES = {"tiny": {"in": 1.0, "out": 0.0}}


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> dict:
    """The firing's clock: a test moves it by setting ``clock["now"]``."""
    held = {"now": NOW}
    monkeypatch.setattr(workflow_firing, "_now", lambda: held["now"])
    return held


@pytest.fixture
def fake(chat):
    server = chat(repeat=text_reply("hi", usage=DOLLAR))
    save_config(Config(openai_base_url=server.url, prices=PRICES))
    return server


def _keep(
    tmp_path, *, name: str = "nightly", ceiling: dict | None = None, scheduled: bool = True, **top
) -> Workflow:
    data = {
        "format": FORMAT,
        "name": name,
        "cwd": str(tmp_path),
        "steps": [
            {"name": "say", "kind": "ask", "prompt": "Say hi.", "engine": "openai", "model": "tiny"}
        ],
        "schedule": {"cron": "0 2 * * *", "tz": "UTC"},
        "ceiling": ceiling or {"firings": 40},
        **top,
    }
    if not scheduled:
        data.pop("schedule"), data.pop("ceiling")
    return store_workflow(json.dumps(data).encode())


def _approved(tmp_path, **kw) -> Workflow:
    wf = _keep(tmp_path, **kw)
    approve(wf)
    return wf


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _envelope(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verdict(envelope: dict, check: str) -> dict[str, str]:
    [found] = [v for v in envelope["verdicts"] if v["check"] == check]
    return dict(part.split("=", 1) for part in found["detail"].split(";"))


def _fire(rid: str) -> Firing:
    return asyncio.run(fire(rid))


def _seals() -> list:
    folder = kept_receipts()
    return sorted(folder.glob(f"{SEAL_PREFIX}-*.json")) if folder.is_dir() else []


# ── saving a routine on a workflow ──────────────────────────────────────────


def test_a_routine_on_a_workflow_pins_its_sha256_and_takes_its_schedule(tmp_path, capsys):
    wf = _approved(tmp_path, ceiling={"firings": 40, "month_cap": "20"})
    code, out, err = _cli(capsys, "workflow", "schedule", "nightly")
    assert code == 0, err
    record = get_routine("nightly")
    assert record["workflow"] == wf.sha256
    assert (record["cron"], record["tz"]) == ("0 2 * * *", "UTC")
    assert out.startswith(f"saved routine nightly ({record['id']}), on; next due ")
    assert f"runs workflow nightly {wf.short} on a schedule under a ceiling you approved" in out
    assert "40 firings; $20.00 a month" in out
    for word in ("auto", "autonomous"):
        assert word not in out.lower()


def test_a_routine_takes_a_workflow_by_its_sha256_too(tmp_path, capsys):
    wf = _approved(tmp_path)
    assert _cli(capsys, "workflow", "schedule", "--name", "n2", wf.sha256[:12])[0] == 0
    assert get_routine("n2")["workflow"] == wf.sha256


@pytest.mark.parametrize("extra", [["--cron", "* * * * *"], ["-e", "openai"], ["Do", "more."]])
def test_a_routine_on_a_workflow_takes_nothing_the_file_does_not_say(tmp_path, capsys, extra):
    _approved(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        main(["workflow", "schedule", "nightly", *extra])
    assert stopped.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
    assert routines() == []


def test_a_routine_is_refused_on_a_workflow_that_is_not_approved(tmp_path, capsys):
    _keep(tmp_path)
    code, _out, err = _cli(capsys, "workflow", "schedule", "nightly")
    assert code == 2 and "routine not saved: workflow: it is not approved" in err
    assert routines() == []


def test_a_routine_is_refused_on_a_workflow_with_no_schedule(tmp_path, capsys):
    approve(_keep(tmp_path, scheduled=False))
    code, _out, err = _cli(capsys, "workflow", "schedule", "nightly")
    assert code == 2 and "has no schedule" in err and routines() == []


def test_a_routine_on_a_workflow_takes_the_name_of_an_inline_one_in_its_place(tmp_path, capsys):
    from test_serve import _save

    rid = _save(tmp_path, "0 9 * * *", name="nightly")
    _approved(tmp_path)
    code, out, err = _cli(capsys, "workflow", "schedule", "nightly")
    assert code == 0 and out.startswith(f"updated routine nightly ({rid})"), err
    record = get_routine("nightly")
    assert "goal" not in record and record["workflow"]


# ── a firing ────────────────────────────────────────────────────────────────


def test_a_firing_runs_the_approved_workflow_and_its_seal_signs_the_lineage(tmp_path, capsys, fake):
    wf = _approved(tmp_path, ceiling={"firings": 40, "expires": "2027-01-01", "month_cap": "20"})
    [approval] = approval_state(wf.sha256).in_force
    rid = save_workflow_routine("nightly", "nightly")
    firing = asyncio.run(fire(rid, due=datetime(2026, 11, 4, 2, 0, tzinfo=UTC)))
    assert (firing.state, firing.reason, firing.workflow, firing.number) == (
        "sealed", None, wf.sha256, 1,
    )  # fmt: skip
    assert firing.approval == approval.run_id
    assert [r["messages"][-1]["content"] for r in fake.requests] == ["Say hi."]
    assert verify_file(firing.receipt).valid
    envelope = _envelope(firing.receipt)
    assert envelope["run_id"] == firing.run_id and firing.run_id.startswith(SEAL_PREFIX)
    assert _verdict(envelope, FIRING_CHECK) == {
        "sha256": wf.sha256,
        "approval": approval.run_id,
        "approval_receipt": approval.digest,
        "routine": rid,
        "firing": "1",
        "firing_id": firing.firing_id,
        "due": "2026-11-04T02:00:00Z",
        "firings_used": "0",
        "firings_max": "40",
        "expires": "2027-01-01",
        "month": "2026-11",
        "month_spent_musd": "0",
        "month_cap_musd": "20000000",
    }
    run = _verdict(envelope, "workflow_run")
    assert run["sha256"] == wf.sha256 and run["spent_musd"] == "1000000"
    code, out, _err = _cli(capsys, "receipts", "verify", firing.run_id)
    assert code == 0 and out.startswith("VALID") and "each present, unchanged, and valid" in out


def test_changed_bytes_refuse_the_firing_and_nothing_runs(tmp_path, fake):
    wf = _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    wf.path.write_bytes(wf.raw.replace(b"Say hi.", b"Say rm."))
    firing = _fire(rid)
    assert firing.state == "refused" and firing.run_id is None
    assert f"the workflow kept as {wf.short} has changed since it was approved" in firing.reason
    assert fake.requests == [] and _seals() == []


def test_a_kept_file_that_is_gone_refuses_the_firing(tmp_path, fake):
    wf = _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    wf.path.unlink()
    firing = _fire(rid)
    assert firing.state == "refused" and "cannot be read" in firing.reason
    assert fake.requests == []


def test_a_revoked_approval_refuses_the_firing(tmp_path, fake):
    wf = _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    revoke(wf.sha256, approval_state(wf.sha256).in_force)
    firing = _fire(rid)
    assert firing.state == "refused" and "its approval was revoked" in firing.reason
    assert fake.requests == [] and _seals() == []


def test_an_approval_of_another_version_never_fires_the_pinned_one(tmp_path, home, fake):
    wf = _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    other = _approved(tmp_path, about="the same name, other bytes")
    revoke(wf.sha256, approval_state(wf.sha256).in_force)
    table = json.loads((home / "routines.json").read_text())
    assert table[rid]["workflow"] == wf.sha256  # pinned: the new version never moves it
    firing = _fire(rid)
    assert firing.state == "refused" and fake.requests == []
    assert approval_state(other.sha256).in_force  # in force, and for other bytes


def test_a_new_version_kept_under_the_name_leaves_the_routine_on_its_sha256(tmp_path, fake):
    wf = _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    _keep(tmp_path, about="v2, never approved")
    firing = _fire(rid)
    assert firing.state == "sealed" and firing.workflow == wf.sha256


def test_a_schedule_edited_by_hand_is_not_the_one_approved(tmp_path, home, fake):
    _approved(tmp_path)
    rid = save_workflow_routine("nightly", "nightly")
    table = json.loads((home / "routines.json").read_text())
    table[rid]["cron"] = "* * * * *"
    (home / "routines.json").write_text(json.dumps(table))
    firing = _fire(rid)
    assert firing.state == "refused" and "not the schedule you approved" in firing.reason
    assert fake.requests == []


def test_a_workflow_with_no_ceiling_never_fires_on_a_schedule(tmp_path, home, fake):
    bare = _keep(tmp_path, scheduled=False)
    approve(bare)
    _approved(tmp_path, name="other")
    rid = save_workflow_routine("nightly", "other")
    table = json.loads((home / "routines.json").read_text())
    table[rid]["workflow"] = bare.sha256  # edited by hand onto a workflow with no ceiling
    (home / "routines.json").write_text(json.dumps(table))
    firing = _fire(rid)
    assert firing.state == "refused" and "needs a schedule and a ceiling" in firing.reason
    assert fake.requests == []


def test_another_firing_of_the_workflow_running_refuses_this_one(tmp_path, fake):
    wf = _approved(tmp_path)
    one = save_workflow_routine("one", "nightly")
    two = save_workflow_routine("two", "nightly")
    _append(Firing("fr-00000000000a", one, None, "running", "wff-x", None, "t", workflow=wf.sha256))
    firing = _fire(two)
    assert firing.state == "refused"
    assert "another firing of this workflow is running" in firing.reason
    assert fake.requests == []


# ── the ceiling ─────────────────────────────────────────────────────────────


def test_firings_used_up_refuse_the_next_one(tmp_path, fake):
    _approved(tmp_path, ceiling={"firings": 2})
    rid = save_workflow_routine("nightly", "nightly")
    first, second = _fire(rid), _fire(rid)
    assert (first.number, second.number) == (1, 2)
    assert _verdict(_envelope(second.receipt), FIRING_CHECK)["firings_used"] == "1"
    third = _fire(rid)
    assert third.state == "refused" and third.reason == "firings used up: 2 of 2"
    assert len(fake.requests) == 2


def test_the_firings_count_is_the_workflow_s_across_its_routines(tmp_path, fake):
    _approved(tmp_path, ceiling={"firings": 1})
    one = save_workflow_routine("one", "nightly")
    two = save_workflow_routine("two", "nightly")
    assert _fire(one).state == "sealed"
    assert _fire(two).reason == "firings used up: 1 of 1"


def test_the_firings_count_holds_with_the_log_or_the_receipts_gone(tmp_path, home, fake):
    _approved(tmp_path, ceiling={"firings": 1})
    rid = save_workflow_routine("nightly", "nightly")
    fired = _fire(rid)
    log = home / "firings.jsonl"
    kept = log.read_text()
    log.unlink()  # the receipts still count it
    assert _fire(rid).reason == "firings used up: 1 of 1"
    log.write_text(kept)
    for path in _seals():  # the log still counts it
        path.unlink()
    assert not (kept_receipts() / f"{fired.run_id}.json").exists()
    assert _fire(rid).reason.startswith("firings used up: 1 of 1")
    assert len(fake.requests) == 1


def test_an_expired_ceiling_refuses_from_that_day_in_its_zone(tmp_path, clock, fake):
    _approved(tmp_path, ceiling={"expires": "2026-11-05"}, schedule={"cron": "0 2 * * *",
              "tz": "Asia/Karachi"})  # fmt: skip
    rid = save_workflow_routine("nightly", "nightly")
    clock["now"] = datetime(2026, 11, 4, 18, 59, tzinfo=UTC)  # 23:59 on the 4th in Karachi
    assert _fire(rid).state == "sealed"
    clock["now"] = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)  # 00:00 on the 5th there
    firing = _fire(rid)
    assert firing.state == "refused" and firing.reason == "expired: its ceiling ended on 2026-11-05"
    assert len(fake.requests) == 1


def test_this_month_s_money_at_the_cap_refuses_and_a_new_month_fires(tmp_path, clock, fake):
    _approved(tmp_path, ceiling={"month_cap": "1"})
    rid = save_workflow_routine("nightly", "nightly")
    assert _fire(rid).state == "sealed"  # spends $1.00
    firing = _fire(rid)
    assert firing.state == "refused"
    assert firing.reason == "this month's money is at or over the cap: $1.00 of $1.00 in 2026-11"
    clock["now"] = datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC)
    fired = _fire(rid)
    assert fired.state == "sealed"
    assert _verdict(_envelope(fired.receipt), FIRING_CHECK)["month_spent_musd"] == "0"
    assert len(fake.requests) == 2


def test_the_cap_holds_inside_a_firing_too(tmp_path, fake):
    _approved(tmp_path, ceiling={"month_cap": "1.50"})
    rid = save_workflow_routine("nightly", "nightly")
    assert _fire(rid).state == "sealed"  # $1.00 of $1.50
    second = _fire(rid)
    [member] = _envelope(second.receipt)["steps"]
    step = _envelope(kept_receipts() / f"{member['call_id']}.json")
    assert _verdict(step, "budget")["cap_musd"] == "500000"  # what was left of the month's cap
    state = ceiling_state(load_workflow(second.workflow), NOW)
    assert state.spent_musd is not None and state.spent_musd >= 1_500_000
    assert _fire(rid).state == "refused"


def _forged_seal(
    wf: Workflow, *, month: str = "2026-11", spent: str = "100", tail: str = "00000a"
) -> str:
    """A firing seal of ``wf`` signed by this home's key, as a firing that ran would seal it."""
    key, _did = ensure_key()
    run_id = f"{SEAL_PREFIX}-{wf.sha256[:16]}-20261101T000000Z-{tail}"
    detail = (
        f"sha256={wf.sha256};approval=wfa-x;approval_receipt=0;routine=rt-000000000000;firing=1;"
        f"firing_id=fr-x;due=none;firings_used=0;firings_max=none;expires=none;month={month};"
        "month_spent_musd=0;month_cap_musd=1000000"
    )
    seal_and_store(
        run_id=run_id, steps=[], started_at="2026-11-01T00:00:00Z",
        finished_at="2026-11-01T00:00:00Z", key=key, index=False,
        verdicts=[
            {"check": "seal", "result": "pass", "detail": "kind=workflow;name=nightly;members=0"},
            {"check": "workflow_run", "result": "pass",
             "detail": f"sha256={wf.sha256};approval=wfa-x;approval_receipt=0;steps=1;ran=0;"
                       f"spent_musd={spent}"},
            {"check": FIRING_CHECK, "result": "pass", "detail": detail},
        ],
    )  # fmt: skip
    return run_id


def test_the_month_s_money_is_the_sum_its_receipts_sign(tmp_path):
    wf = _approved(tmp_path, ceiling={"month_cap": "1"})
    _forged_seal(wf, spent="250000", tail="00000a")
    moved = _forged_seal(wf, spent="300000", tail="00000b")
    _forged_seal(wf, month="2026-10", spent="900000", tail="00000c")  # last month: not counted
    state = ceiling_state(wf, NOW)
    assert (state.unreadable, state.used, state.spent_musd) == ([], 3, 550000)
    renamed = moved.replace("00000b", "00000d")
    (kept_receipts() / f"{moved}.json").rename(kept_receipts() / f"{renamed}.json")
    state = ceiling_state(wf, NOW)  # kept under an id it does not sign
    assert state.unreadable == [renamed]


def test_a_firing_receipt_changed_by_hand_refuses_the_firing(tmp_path, fake):
    wf = _approved(tmp_path, ceiling={"month_cap": "5"})
    rid = save_workflow_routine("nightly", "nightly")
    fired = _fire(rid)
    receipt = Path(fired.receipt)
    receipt.write_text(receipt.read_text().replace("spent_musd=1000000", "spent_musd=1"))
    firing = _fire(rid)
    assert firing.state == "refused"
    assert f"a firing receipt of it does not verify ({fired.run_id})" in firing.reason
    assert ceiling_state(wf, NOW).unreadable == [fired.run_id]
    assert len(fake.requests) == 1


def test_money_not_known_this_month_refuses_a_capped_firing(tmp_path, fake):
    wf = _approved(tmp_path, ceiling={"month_cap": "5"})
    rid = save_workflow_routine("nightly", "nightly")
    seal = _forged_seal(wf, spent="none")
    firing = _fire(rid)
    assert firing.state == "refused"
    assert firing.reason == (
        f"this month's money is not known (firing {seal} spent an amount not known), "
        "so its cap of $5.00 cannot hold"
    )
    assert fake.requests == []


def test_a_firing_cut_off_before_it_sealed_leaves_the_month_s_money_not_known(tmp_path, fake):
    wf = _approved(tmp_path, ceiling={"month_cap": "5"})
    rid = save_workflow_routine("nightly", "nightly")
    _append(Firing("fr-00000000000c", rid, None, "error", "wff-gone", "restart",
                   "2026-11-03T00:00:00Z", workflow=wf.sha256))  # fmt: skip
    firing = _fire(rid)
    assert firing.state == "refused"
    assert "firing fr-00000000000c ran and left no receipt that verifies" in firing.reason
    assert fake.requests == []


def test_a_firing_without_a_month_cap_does_not_wait_on_money(tmp_path, fake):
    wf = _approved(tmp_path, ceiling={"firings": 5})
    rid = save_workflow_routine("nightly", "nightly")
    _forged_seal(wf, spent="none")
    firing = _fire(rid)
    assert firing.state == "sealed" and firing.number == 2
    assert _verdict(_envelope(firing.receipt), FIRING_CHECK)["month_spent_musd"] == "none"


# ── serve ───────────────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _working(sched: Scheduler):
    worker = asyncio.create_task(sched.work())
    try:
        yield sched
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


async def test_serve_fires_a_workflow_routine_when_it_comes_due(tmp_path, fake):
    _approved(tmp_path, ceiling={"firings": 1})
    rid = save_workflow_routine("nightly", "nightly")
    said: list[str] = []
    sched = Scheduler(datetime(2026, 11, 4, 1, 59, tzinfo=UTC), say=said.append)
    async with _working(sched):
        for day in (4, 5):
            sched.tick(datetime(2026, 11, day, 2, 0, 5, tzinfo=UTC))
            await sched.drain()
    rows = [(f.state, f.due, f.reason) for f in firings(rid)]
    assert rows == [
        ("sealed", "2026-11-04T02:00:00Z", None),
        ("refused", "2026-11-05T02:00:00Z", "firings used up: 1 of 1"),
    ]
    assert any("refused (firings used up: 1 of 1)" in line for line in said)


# ── the routines of today, and what lists them ──────────────────────────────


def _inline(tmp_path) -> str:
    from test_serve import _save

    return _save(tmp_path, "0 9 * * *", name="triage")


def test_an_inline_routine_still_fires_and_its_firing_says_not_approved(tmp_path, chat):
    from test_serve import Quick

    rid = _inline(tmp_path)
    firing = asyncio.run(fire(rid, make_engine=lambda name: Quick()))
    assert firing.state == "sealed" and verify_file(firing.receipt).valid
    assert (firing.approval, firing.workflow) == (NOT_APPROVED, None)
    row = json.loads((metabot_home() / "firings.jsonl").read_text().splitlines()[-1])
    assert row["approval"] == "not approved"
    journal = json.loads((metabot_home() / "runs" / firing.run_id / "journal.json").read_text())
    assert journal["meta"]["approval"] == "not approved"


def test_an_old_firing_row_reads_as_not_approved(tmp_path, home, capsys):
    rid = _inline(tmp_path)
    old = {"firing_id": "fr-00000000000d", "routine_id": rid, "due": None, "state": "sealed",
           "run_id": "mb-x", "reason": None, "at": "2026-01-01T00:00:00Z"}  # fmt: skip
    (home / "firings.jsonl").write_text(json.dumps(old) + "\n")
    [firing] = firings(rid)
    assert firing.approval is None
    code, out, _err = _cli(capsys, "routine", "firings")
    assert code == 0 and "not approved" in out


def test_list_show_firings_and_status_label_each_routine(tmp_path, capsys, fake):
    from test_serve import Quick

    wf = _approved(tmp_path, ceiling={"firings": 40, "month_cap": "20"})
    [approval] = approval_state(wf.sha256).in_force
    save_workflow_routine("nightly", "nightly")
    asyncio.run(fire(get_routine("nightly")["id"]))
    asyncio.run(fire(_inline(tmp_path), make_engine=lambda name: Quick()))

    code, out, _err = _cli(capsys, "routine", "list")
    [nightly] = [line for line in out.splitlines() if line.startswith("nightly")]
    [triage] = [line for line in out.splitlines() if line.startswith("triage")]
    assert f"workflow nightly {wf.short}, approved" in nightly
    assert "firings 1 of 40" in nightly and "$1.00 of $20.00 this month" in nightly
    assert triage.rstrip().endswith("not approved")

    code, out, _err = _cli(capsys, "routine", "show", "nightly")
    assert code == 0
    assert f"runs      workflow nightly {wf.sha256}" in out
    assert f"approval  approved at {approval.signed_at} (receipt {approval.run_id})" in out
    assert "ceiling   40 firings; $20.00 a month" in out
    assert "used      1 of 40 firings; $1.00 of $20.00 in 2026-11" in out
    assert "on a schedule under a ceiling you approved" in out
    code, out, _err = _cli(capsys, "routine", "show", "triage")
    assert code == 0 and "approval  not approved" in out and "runs      goal on openai" in out

    code, out, _err = _cli(capsys, "routine", "firings")
    [wf_row] = [line for line in out.splitlines() if "nightly" in line]
    [in_row] = [line for line in out.splitlines() if "triage" in line]
    assert f"workflow {wf.short} firing 1" in wf_row and f"approval {approval.run_id}" in wf_row
    assert "not approved" in in_row

    code, out, _err = _cli(capsys, "status")
    assert "scheduled workflows (counted from receipts that verify)" in out
    [line] = [line for line in out.splitlines() if line.lstrip().startswith("nightly")]
    assert f"workflow nightly {wf.short}" in line and "approved" in line
    assert "firings 1 of 40" in line and "$1.00 of $20.00 in 2026-11" in line
    assert "routines not approved" in out
    [triage] = [line for line in out.splitlines() if line.lstrip().startswith("triage")]
    assert "not approved" in triage
    assert "nothing is running" in out  # live state, as before
    code, out, _err = _cli(capsys, "status", "--json")
    shown = json.loads(out)
    [scheduled] = shown["scheduled"]
    assert (scheduled["name"], scheduled["firings_used"], scheduled["month_spent_musd"]) == (
        "nightly", 1, 1_000_000,
    )  # fmt: skip
    assert [r["name"] for r in shown["not_approved"]] == ["triage"]
    assert shown["alive"] == [] and shown["stale"] == []


def test_status_with_a_routines_file_that_does_not_read_still_shows_what_runs(home, capsys):
    home.mkdir(parents=True, exist_ok=True)
    (home / "routines.json").write_text("[1, 2]")
    code, out, _err = _cli(capsys, "status")
    assert code == 0 and "nothing is running" in out and "cannot read the routines" in out


def test_the_routines_page_labels_each_routine(tmp_path, fake):
    from test_web_pages import _client

    wf = _approved(tmp_path)
    save_workflow_routine("nightly", "nightly")
    _inline(tmp_path)
    page = _client(tmp_path).get("/routines")
    assert page.status_code == 200
    assert f"workflow nightly {wf.short}, approved" in page.text
    assert "not approved" in page.text
    assert f'<a href="/workflows/{wf.sha256}">its workflow\'s steps</a>' in page.text
    assert _client(tmp_path).get(f"/workflows/{wf.sha256}").status_code == 200


def test_show_says_serve_fires_a_workflow_a_routine_names(tmp_path, capsys):
    _keep(tmp_path, ceiling={"expires": "2099-12-31"})
    code, out, _err = _cli(capsys, "workflow", "show", "nightly")
    assert code == 0 and "serve does not fire" not in out
    assert "serve fires it once a routine names it: metabot workflow schedule nightly" in out
    assert "no firing on or after the day it expires" in out


def test_routine_run_fires_a_workflow_routine_now_under_the_same_checks(tmp_path, capsys, fake):
    wf = _approved(tmp_path, ceiling={"firings": 1})
    save_workflow_routine("nightly", "nightly")
    code, out, err = _cli(capsys, "routine", "run", "-q", "nightly")
    assert code == 0, err
    [firing] = firings(get_routine("nightly")["id"])
    assert f"metabot receipts verify {firing.run_id}" in out + err
    code, _out, err = _cli(capsys, "routine", "run", "-q", "nightly")
    assert code == 1 and "routine not fired: firings used up: 1 of 1" in err
    assert wf.sha256 == firing.workflow and len(fake.requests) == 1


def test_routines_file_keeps_today_s_routines_as_they_were(tmp_path, home):
    rid = _inline(tmp_path)
    record = json.loads((home / "routines.json").read_text())[rid]
    assert "workflow" not in record and record["goal"] == "Say done."


def test_the_copy_keeps_to_plain_words(tmp_path, capsys, fake):
    _approved(tmp_path)
    save_workflow_routine("nightly", "nightly")
    _fire(get_routine("nightly")["id"])
    said = ""
    for argv in (["routine", "list"], ["routine", "show", "nightly"], ["status"],
                 ["routine", "firings"], ["workflow", "show", "nightly"]):  # fmt: skip
        said += _cli(capsys, *argv)[1]
    for word in ("auto", "autonomous", "trustless", "nobody", "web3"):
        assert word not in said.lower(), (word, said)
