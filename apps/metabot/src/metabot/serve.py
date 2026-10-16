"""``metabot serve``: one process fires each routine when it comes due.

One serve at a time: it holds ``serve.lock`` (an ``flock``, with its pid in
the file while it runs), and a second serve refuses, naming the first.

* On start, a firing that a stopped serve left ``running`` is settled. It
  stays ``sealed`` when its run sealed, and is otherwise an ``error`` with
  reason ``restart``. It is never run again.
* Each tick re-reads ``routines.json``, so an edit applies on the next tick.
* A due time is claimed once, and the next one is counted from now. A time
  missed while serve was down or busy is not fired later.
* Firings run one at a time, in the order they came due. A routine that is
  still running when it comes due again is ``skipped``, with the reason.
* SIGTERM or SIGINT stops serve. The firing under way is stopped and its
  receipt sealed; a firing still waiting is ``skipped``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TextIO

from metabot.approvals import printable
from metabot.cron import CronError
from metabot.paths import metabot_home
from metabot.routines import (
    Firing,
    MakeEngine,
    fire,
    firings,
    next_due,
    note_firing,
    routines,
    sweep_restart,
    utc_stamp,
)
from metabot.runner import RunCancelled
from metabot.text import ascii_digits

TICK_S = 15.0
LOCK = "serve.lock"
OVERLAP = "overlap: an earlier firing of this routine is still running"
STOPPED_FIRST = "serve stopped before this firing began"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)

Say = Callable[[str], None]


def _now() -> datetime:
    return datetime.now(UTC)


class ServeLocked(RuntimeError):
    def __init__(self, pid: str) -> None:
        super().__init__(f"serve is already running (pid {pid})")
        self.pid = pid


@contextlib.contextmanager
def serve_lock() -> Iterator[None]:
    """Hold ``serve.lock`` while one serve runs; ServeLocked names the pid holding it."""
    fd = os.open(metabot_home() / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            raise ServeLocked(held if ascii_digits(held) else "unknown") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)  # once it stops, its pid may be another process's
    finally:
        os.close(fd)  # closing it lets the lock go


def _signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return (record.get("cron"), record.get("tz"), record.get("enabled"), record.get("saved_at"))


class Scheduler:
    """Which routines are due, each due time claimed once; the firings, one at a time."""

    def __init__(
        self, started: datetime, *, make_engine: MakeEngine | None = None, say: Say | None = None
    ) -> None:
        self.last = started  # a routine seen for the first time counts its due times from here
        self.make_engine = make_engine
        self.say: Say = say or (lambda text: None)
        self.next: dict[str, datetime] = {}
        self.seen: dict[str, tuple[Any, ...]] = {}
        self.broken: dict[str, tuple[Any, ...]] = {}
        self.names: dict[str, str] = {}
        self.queue: asyncio.Queue[tuple[str, datetime]] = asyncio.Queue()
        self.busy: set[str] = set()  # waiting or firing

    def _name(self, rid: str) -> str:
        return printable(self.names.get(rid, rid), 64)

    def _note(self, rid: str, due: datetime | None, state: str, reason: str) -> Firing:
        firing = note_firing(rid, due, state, reason)
        self.say(f"{self._name(rid)}: {state} ({printable(reason, 300)})")
        return firing

    def tick(self, now: datetime) -> None:
        """Claim what is due at ``now``; the claimed firings wait for :meth:`work`."""
        try:
            table = {str(r["id"]): r for r in routines() if "id" in r}
        except (OSError, ValueError) as exc:
            self.say(f"cannot read the routines: {printable(str(exc), 300)}")
            return
        for rid in set(self.next) - set(table):
            del self.next[rid]
        due_now: list[tuple[datetime, str, str]] = []
        for rid, record in table.items():
            self.names[rid] = str(record.get("name"))
            signature = _signature(record)
            if not record.get("enabled"):
                self.next.pop(rid, None)
                self.seen[rid] = signature
                continue
            if self.seen.get(rid) != signature or rid not in self.next:
                self.seen[rid] = signature
                try:
                    self.next[rid] = next_due(record, self.last)
                except (CronError, KeyError, TypeError, ValueError) as exc:
                    self.next.pop(rid, None)
                    if self.broken.get(rid) != signature:  # once, until it is edited
                        self.broken[rid] = signature
                        self._note(rid, None, "error", f"invalid schedule: {exc}")
                    continue
            due = self.next[rid]
            if due > now:
                continue
            try:
                self.next[rid] = next_due(record, now)  # claimed: the next time counts from now
            except (CronError, KeyError, TypeError, ValueError):
                self.next.pop(rid, None)
            due_now.append((due, self.names[rid], rid))
        for due, _, rid in sorted(due_now):
            self._claim(rid, due)
        self.last = now

    def _claim(self, rid: str, due: datetime) -> None:
        earlier = firings(rid)
        if any(f.due == utc_stamp(due) for f in earlier):
            return  # this due time already has its firing
        if rid in self.busy or any(f.state == "running" for f in earlier):
            self._note(rid, due, "skipped", OVERLAP)
            return
        self.busy.add(rid)
        self.queue.put_nowait((rid, due))

    def _said(self, firing: Firing) -> None:
        parts = [f"{self._name(firing.routine_id)}: {firing.state}"]
        if firing.reason:
            parts.append(f" ({printable(firing.reason, 300)})")
        if firing.receipt:
            parts.append(f"; receipt {firing.receipt}")
        if firing.acceptance:
            parts.append(f"; declared acceptance {firing.acceptance}")
        self.say("".join(parts))

    async def work(self) -> None:
        """Fire what was claimed, one at a time, until cancelled."""
        while True:
            rid, due = await self.queue.get()
            try:
                self._said(await fire(rid, due=due, make_engine=self.make_engine))
            except RunCancelled as exc:  # serve is stopping: the run was stopped and sealed
                self.say(f"{self._name(rid)}: stopped; receipt {exc.result.receipt}")
                raise
            except KeyError:
                self.say(f"{self._name(rid)}: removed before it fired")
            except Exception as exc:  # one broken firing must not stop the others
                self.say(f"{self._name(rid)}: {type(exc).__name__}: {printable(str(exc), 300)}")
            finally:
                self.busy.discard(rid)
                self.queue.task_done()

    async def drain(self) -> None:
        """Wait until every claimed firing has run."""
        await self.queue.join()

    def stop(self) -> None:
        """Once the worker is cancelled: a firing still waiting never began."""
        while not self.queue.empty():
            rid, due = self.queue.get_nowait()
            self.queue.task_done()
            self.busy.discard(rid)
            self._note(rid, due, "skipped", STOPPED_FIRST)


async def _until(aw: Awaitable[Any], stop: asyncio.Event) -> None:
    """Wait for ``aw`` or for ``stop``, whichever comes first."""
    work = asyncio.ensure_future(aw)
    halt = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({work, halt}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (work, halt):
            task.cancel()
        await asyncio.gather(work, halt, return_exceptions=True)


async def _serve(
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    once: bool,
    make_engine: MakeEngine | None,
    say: Say,
) -> int:
    for firing in sweep_restart():
        said = f" ({printable(firing.reason, 200)})" if firing.reason else ""
        say(f"firing {firing.firing_id}, left running when serve stopped, is {firing.state}{said}")
    sched = Scheduler(clock(), make_engine=make_engine, say=say)
    on = sum(1 for r in routines() if r.get("enabled"))
    say(f"serve (pid {os.getpid()}): {on} routine(s) on; a stop signal ends it")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled = []
    for sig in STOP_SIGNALS:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
            handled.append(sig)
    worker = asyncio.create_task(sched.work())
    try:
        while not stop.is_set():
            try:
                sched.tick(clock())
            except Exception as exc:  # a bad tick must not end serve; the next one tries again
                say(f"tick failed: {type(exc).__name__}: {printable(str(exc), 300)}")
            await _until(sched.drain() if once else sleep(TICK_S), stop)
            if once:
                break
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        sched.stop()
        for sig in handled:
            loop.remove_signal_handler(sig)
    say("serve stopped")
    return 0


async def serve(
    *,
    clock: Callable[[], datetime] = _now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    once: bool = False,
    make_engine: MakeEngine | None = None,
    out: TextIO | None = None,
) -> int:
    """Fire routines as they come due until a stop signal; 2 when another serve runs.

    ``once`` runs one tick and waits for the firings it claimed.
    """
    stream = out or sys.stderr

    def say(text: str) -> None:
        print(f"[metabot] {text}", file=stream, flush=True)

    try:
        with serve_lock():
            return await _serve(clock, sleep, once, make_engine, say)
    except ServeLocked as exc:
        print(f"metabot: {exc}; one serve fires every routine", file=stream, flush=True)
        return 2


def cmd_serve(_args: argparse.Namespace) -> int:
    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:  # before the stop signals were taken over
        return 130
