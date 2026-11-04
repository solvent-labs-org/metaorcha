"""``metabot serve``: one process, one lock, each due time fired once, nothing backfilled."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest

from metabot.cli import main
from metabot.events import Final, Started
from metabot.receipts import verify_file
from metabot.routines import (
    Firing,
    RoutineSpec,
    _append,
    fire,
    firings,
    get_routine,
    save_routine,
    set_enabled,
)
from metabot.serve import OVERLAP, STOPPED_FIRST, Scheduler, ServeLocked, serve, serve_lock


def at(hour: int, minute: int, second: int = 0, day: int = 3) -> datetime:
    return datetime(2031, 3, day, hour, minute, second, tzinfo=UTC)


def slot(hour: int, minute: int, day: int = 3) -> str:
    return f"2031-03-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def ticking(*times: datetime):
    """A clock that gives each time in turn, then keeps giving the last."""
    left = list(times)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


def _save(tmp_path: Path, cron: str, name: str = "tick", rid: str | None = None) -> str:
    spec = RoutineSpec(
        name=name, goal="Say done.", engine="openai", model=None, cwd=str(tmp_path),
        connections=[], scope_allow=[], criteria={}, cron=cron, tz="UTC",
    )  # fmt: skip
    return save_routine(spec, list_tools=lambda conn: [], routine_id=rid)


class Quick:
    """An in-process engine that answers at once."""

    name = "quick"
    tool_prefix = "quick"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        yield Final("done", "s-1")


class Waits(Quick):
    """An in-process engine that answers once the test lets it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        self.started.set()
        await self.go.wait()
        yield Final("done", "s-1")


@contextlib.asynccontextmanager
async def working(sched: Scheduler):
    worker = asyncio.create_task(sched.work())
    try:
        yield sched
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


def _rows(rid: str) -> list[tuple[str, str | None, str | None]]:
    return [(f.state, f.due, f.reason) for f in firings(rid)]


async def test_due_routine_fires_once_per_due_time(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    sched = Scheduler(at(8, 59, 30), make_engine=lambda name: Quick())
    async with working(sched):
        for now in (at(9, 0), at(9, 0, 15), at(9, 0, 45), at(9, 1)):
            sched.tick(now)
            await sched.drain()
    assert _rows(rid) == [("sealed", slot(9, 0), None)]
    [firing] = firings(rid)
    assert verify_file(firing.receipt).valid


async def test_missed_times_are_not_backfilled(tmp_path):
    early = _save(tmp_path, "0 9 * * *", name="early")
    every = _save(tmp_path, "* * * * *", name="every")
    sched = Scheduler(at(9, 0, 30), make_engine=lambda name: Quick())  # serve was down at 09:00
    async with working(sched):
        for now in (at(9, 0, 45), at(9, 5, 10), at(9, 5, 50)):
            sched.tick(now)
            await sched.drain()
    assert firings(early) == []
    # 09:01 to 09:05 passed in one late tick: the first fires, late, and the rest are dropped
    assert _rows(every) == [("sealed", slot(9, 1), None)]


async def test_overlap_is_skipped_with_a_reason(tmp_path):
    rid = _save(tmp_path, "* * * * *")
    engine = Waits()
    sched = Scheduler(at(9, 0, 30), make_engine=lambda name: engine)
    async with working(sched):
        sched.tick(at(9, 1))
        await engine.started.wait()
        sched.tick(at(9, 2))  # 09:02 comes due while the 09:01 firing still runs
        engine.go.set()
        await sched.drain()
    assert _rows(rid) == [("sealed", slot(9, 1), None), ("skipped", slot(9, 2), OVERLAP)]


async def test_a_firing_running_elsewhere_is_an_overlap_too(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    _append(Firing("fr-0123456789ab", rid, None, "running", "mb-elsewhere", None, "now"))
    sched = Scheduler(at(8, 59), make_engine=lambda name: pytest.fail("fired"))
    async with working(sched):
        sched.tick(at(9, 0))
        await sched.drain()
    assert _rows(rid)[-1] == ("skipped", slot(9, 0), OVERLAP)


async def test_firings_run_one_at_a_time_and_a_waiting_one_is_an_overlap_too(tmp_path):
    first = _save(tmp_path, "* * * * *", name="a-first")
    second = _save(tmp_path, "* * * * *", name="b-second")
    engines = {"a-first": Waits(), "b-second": Waits()}
    names = iter(["a-first", "b-second"])
    sched = Scheduler(at(9, 0, 30), make_engine=lambda name: engines[next(names)])
    async with working(sched):
        sched.tick(at(9, 1))
        await engines["a-first"].started.wait()
        await asyncio.sleep(0.05)
        assert not engines["b-second"].started.is_set()  # waits for the first to finish
        sched.tick(at(9, 2))  # both come due again: one firing, one waiting
        engines["a-first"].go.set()
        engines["b-second"].go.set()
        await sched.drain()
    assert _rows(first) == [("sealed", slot(9, 1), None), ("skipped", slot(9, 2), OVERLAP)]
    assert _rows(second) == [("skipped", slot(9, 2), OVERLAP), ("sealed", slot(9, 1), None)]


async def test_a_due_time_that_already_fired_is_not_fired_again(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    _append(Firing("fr-0123456789ab", rid, slot(9, 0), "sealed", "mb-before", None, "t"))
    sched = Scheduler(at(8, 59), make_engine=lambda name: pytest.fail("fired twice"))
    async with working(sched):
        sched.tick(at(9, 0))  # say the clock was set back after that firing
        await sched.drain()
    assert _rows(rid) == [("sealed", slot(9, 0), None)]


async def test_edits_apply_on_the_next_tick(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    sched = Scheduler(at(8, 0), make_engine=lambda name: Quick())
    async with working(sched):
        sched.tick(at(8, 30))
        _save(tmp_path, "45 8 * * *", rid=rid)  # moved earlier between two ticks
        sched.tick(at(8, 45))
        await sched.drain()
        set_enabled(rid, False)  # switched off between two ticks
        sched.tick(at(8, 45, day=4))
        await sched.drain()
    assert _rows(rid) == [("sealed", slot(8, 45), None)]


async def test_a_schedule_that_no_longer_reads_is_an_error_once(tmp_path, home):
    rid = _save(tmp_path, "0 9 * * *")
    table = json.loads((home / "routines.json").read_text())
    table[rid]["cron"] = "61 * * * *"  # edited by hand
    (home / "routines.json").write_text(json.dumps(table))
    sched = Scheduler(at(8, 0), make_engine=lambda name: pytest.fail("fired"))
    async with working(sched):
        sched.tick(at(8, 1))
        sched.tick(at(8, 2))
    [(state, due, reason)] = _rows(rid)
    assert state == "error" and due is None and reason.startswith("invalid schedule:")


@pytest.mark.parametrize(
    ("key", "value"), [("cron", 5), ("cron", ["0 9 * * *"]), ("tz", 5), ("tz", ["UTC"])]
)
async def test_a_schedule_of_another_type_is_an_error_and_the_others_still_fire(
    tmp_path, home, key, value
):
    good = _save(tmp_path, "0 9 * * *", name="good")
    bad = _save(tmp_path, "0 9 * * *", name="bad")
    table = json.loads((home / "routines.json").read_text())
    table[bad][key] = value  # edited by hand
    (home / "routines.json").write_text(json.dumps(table))
    sched = Scheduler(at(8, 59, 30), make_engine=lambda name: Quick())
    async with working(sched):
        sched.tick(at(9, 0))
        await sched.drain()
    assert _rows(good) == [("sealed", slot(9, 0), None)]
    [(state, _, reason)] = _rows(bad)
    assert state == "error" and reason.startswith("invalid schedule:")


async def test_second_serve_refuses_on_the_lock():
    out = io.StringIO()
    with serve_lock():  # held here, as a running serve holds it
        assert await serve(once=True, out=out) == 2
    assert f"serve is already running (pid {os.getpid()})" in out.getvalue()


def test_the_command_refuses_on_the_lock_too(capsys):
    with serve_lock():
        assert main(["serve"]) == 2
    assert f"(pid {os.getpid()})" in capsys.readouterr().err


def test_a_stopped_serve_leaves_no_pid_in_the_lock(home):
    lock = home / "serve.lock"
    with serve_lock():
        assert lock.read_text(encoding="ascii") == f"{os.getpid()}\n"
        with pytest.raises(ServeLocked), serve_lock():  # a second serve, refused
            pass
        assert lock.read_text(encoding="ascii") == f"{os.getpid()}\n"
    assert lock.read_text(encoding="ascii") == ""  # nobody to signal: that pid may be reused


async def test_serve_waits_for_its_worker_to_end_before_it_stops(home, monkeypatch):
    order: list[str] = []
    work, stop = Scheduler.work, Scheduler.stop

    async def working(self: Scheduler) -> None:
        try:
            await work(self)
        finally:
            order.append("worker ended")

    def stopping(self: Scheduler) -> None:
        order.append("stop")
        stop(self)

    monkeypatch.setattr(Scheduler, "work", working)
    monkeypatch.setattr(Scheduler, "stop", stopping)
    assert await serve(clock=ticking(at(8, 0), at(8, 0, 15)), once=True, out=io.StringIO()) == 0
    assert order == ["worker ended", "stop"]


async def test_restart_turns_running_into_error_restart(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    _append(Firing("fr-00000000000a", rid, slot(9, 0), "running", "mb-never-sealed", None, "t"))
    sealed = await fire(rid, make_engine=lambda name: Quick())
    _append(Firing("fr-00000000000b", rid, None, "running", sealed.run_id, None, "t"))
    assert await serve(clock=ticking(at(8, 0), at(8, 0, 15)), once=True, out=io.StringIO()) == 0
    latest = {f.firing_id: f for f in firings(rid)}
    assert (latest["fr-00000000000a"].state, latest["fr-00000000000a"].reason) == (
        "error",
        "restart",
    )
    kept = latest["fr-00000000000b"]  # its run sealed before serve stopped: the receipt is kept
    assert kept.state == "sealed" and kept.receipt == sealed.receipt


async def test_serve_once_fires_what_is_due(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    out = io.StringIO()
    clock = ticking(at(8, 59, 50), at(9, 0, 5))
    code = await serve(clock=clock, once=True, out=out, make_engine=lambda name: Quick())
    assert code == 0 and _rows(rid) == [("sealed", slot(9, 0), None)]
    assert "tick: sealed" in out.getvalue()


async def test_sigterm_stops_after_sealing_the_current_firing(tmp_path):
    rid = _save(tmp_path, "0 9 * * *")
    later = _save(tmp_path, "0 9 * * *", name="z-later")  # due with it; its name sorts after
    engine = Waits()
    clock = ticking(at(8, 59, 50), at(9, 0, 5))
    out = io.StringIO()
    task = asyncio.create_task(
        serve(clock=clock, once=True, out=out, make_engine=lambda name: engine)
    )
    await asyncio.wait_for(engine.started.wait(), 30)
    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, None)  # serve's handler
    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, 30) == 0
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # put back
    [stopped] = firings(rid)
    assert (stopped.state, stopped.reason) == ("error", "stopped")
    assert verify_file(stopped.receipt).valid
    assert f"tick: stopped; receipt {stopped.receipt}" in out.getvalue()
    assert _rows(later) == [("skipped", slot(9, 0), STOPPED_FIRST)]  # queued, never begun
    assert get_routine(rid)["enabled"] is True
