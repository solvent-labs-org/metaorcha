"""Story 2.2: a routine fires on its schedule and produces a run.

The scheduler claims a due slot by compare-and-set on ``next_run_at`` before
anything else, writes the ``routine_firings`` row, then creates the session
and runs the routine's goal through the runner a chat turn uses (AD-19,
AD-22, NFR-7). A slot fires at most once; a crash after the claim skips it.
A firing that cannot succeed calls nothing and says why.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from superagent.workflow import firing_rules as rules
from superagent.workflow.scheduler import WorkflowScheduler, record_resume

NOW = datetime(2026, 10, 5, 9, 0, 30, tzinfo=UTC)  # a Monday, 09:00:30 UTC
SLOT = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
DID = "did:orcha:agent:gh-1a2b3c4d"
_ids = itertools.count(1)


# -- an in-memory database: the queries the scheduler makes, nothing more ----


def _matches(row: dict[str, Any], where: dict[str, Any]) -> bool:
    for key, want in where.items():
        have = row.get(key)
        if isinstance(want, dict):
            ((op, arg),) = want.items()
            if op == "in":
                if have not in arg:
                    return False
            elif op == "lte":
                if have is None or have > arg:
                    return False
            else:
                raise AssertionError(f"unsupported operator {op!r}")
        elif have != want:
            return False
    return True


class Table:
    def __init__(self, log: list[str], name: str, unique: tuple[str, ...] = ()) -> None:
        self.rows: list[dict[str, Any]] = []
        self.log = log
        self.name = name
        self.unique = unique

    async def find_many(
        self, where: dict[str, Any], order: Any = None
    ) -> list[SimpleNamespace]:
        # Rows are kept in insertion order, which is created_at ascending.
        assert order in (None, {"created_at": "asc"})
        return [SimpleNamespace(**r) for r in self.rows if _matches(r, where)]

    async def find_first(self, where: dict[str, Any]) -> SimpleNamespace | None:
        found = await self.find_many(where)
        return found[0] if found else None

    async def find_unique(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return await self.find_first(where)

    async def update_many(self, where: dict[str, Any], data: dict[str, Any]) -> int:
        hit = [r for r in self.rows if _matches(r, where)]
        for row in hit:
            row.update(data)
        self.log.append(f"{self.name}.update_many:{len(hit)}")
        return len(hit)

    async def update(
        self, where: dict[str, Any], data: dict[str, Any]
    ) -> SimpleNamespace:
        (row,) = [r for r in self.rows if _matches(r, where)]
        row.update(data)
        self.log.append(f"{self.name}.update:{data.get('state', '-')}")
        return SimpleNamespace(**row)

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        # NULLs are distinct in a Postgres unique index.
        if self.unique and any(
            all(
                data.get(c) is not None and r.get(c) == data.get(c) for c in self.unique
            )
            for r in self.rows
        ):
            raise RuntimeError("unique constraint")
        row = {
            "id": f"{self.name}-{next(_ids)}",
            "run_id": None,
            "session_id": None,
            **data,
        }
        self.rows.append(row)
        self.log.append(f"{self.name}.create:{data.get('state')}")
        return SimpleNamespace(**row)


class DB:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.workflowtemplate = Table(self.log, "routine")
        self.routinefiring = Table(self.log, "firing", unique=("routine_id", "slot"))
        self.officemember = Table(self.log, "member")
        self.agent = Table(self.log, "agent")
        # The settle gate's ledger and the sealed envelopes (story 2.3).
        self.attestedsettlement = Table(
            self.log, "settlement", unique=("settled_run_id",)
        )
        self.attestation = Table(self.log, "attestation", unique=("run_id",))

    def factory(self):
        @contextlib.asynccontextmanager
        async def conn():
            yield self

        return conn


def _seed(db: DB, **over: Any) -> dict[str, Any]:
    routine = {
        "id": "wf-1",
        "user_id": "bob",
        "office_id": "off-1",
        "name": "Weekly digest",
        "goal_template": "Summarise this week's open issues",
        "agents_used": [DID],
        "parameters": {"scope_allow": [f"{DID}#create_comment"], "model": "m/1"},
        "criteria": {"citations_required": True},
        "schedule_cron": "0 9 * * 1",
        "schedule_tz": "UTC",
        "schedule_enabled": True,
        "next_run_at": SLOT,
        **over,
    }
    db.workflowtemplate.rows.append(routine)
    db.officemember.rows.append({"office_id": "off-1", "user_id": "bob"})
    db.agent.rows.append(
        {"id": DID, "user_id": "bob", "office_id": "off-1", "is_active": True}
    )
    return routine


class Runner:
    """Stands in for ``SessionRunner``: records the call, yields scripted events."""

    def __init__(self, events: list[dict[str, Any]] | None = None, log=None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events = (
            events
            if events is not None
            else [
                {"type": "token", "content": "x"},
                {"type": "done", "run_id": "run-1"},
            ]
        )
        self.log = log

    async def run_turn(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.log is not None:
            self.log.append("runner.run_turn")
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def _scheduler(db: DB, runner: Runner | None = None, **kw: Any) -> WorkflowScheduler:
    sessions: list[tuple] = kw.pop("sessions", [])

    async def create_session(*args: Any) -> None:
        sessions.append(args)
        db.log.append("session.create")

    async def no_problem(did: str, user_id: str) -> str | None:
        return None

    return WorkflowScheduler(
        runner=runner if runner is not None else Runner(log=db.log),
        db=db.factory(),
        check_connection=kw.pop("check_connection", no_problem),
        create_session=create_session,
        flags=kw.pop("flags", lambda: (True, True)),
        clock=lambda: NOW,
    )


async def _tick(scheduler: WorkflowScheduler) -> None:
    tasks = await scheduler._tick()
    if tasks:
        await asyncio.gather(*tasks)


def _firings(db: DB) -> list[tuple[Any, ...]]:
    return [(f["slot"], f["state"], f["detail"]) for f in db.routinefiring.rows]


# -- firing_rules -------------------------------------------------------------


def test_next_slot_is_strictly_after_and_in_utc() -> None:
    assert rules.next_slot("0 9 * * 1", "UTC", SLOT) == SLOT + timedelta(days=7)
    assert rules.next_slot("0 9 * * 1", "UTC", SLOT - timedelta(seconds=1)) == SLOT


def test_next_slot_follows_the_routine_time_zone_across_dst() -> None:
    # 09:00 London is 08:00 UTC in summer and 09:00 UTC after the clocks go back.
    before = datetime(2026, 10, 20, tzinfo=UTC)
    assert rules.next_slot("0 9 * * 1", "Europe/London", before) == datetime(
        2026, 10, 26, 9, 0, tzinfo=UTC
    )
    assert rules.next_slot(
        "0 9 * * 1", "Europe/London", datetime(2026, 10, 12, tzinfo=UTC)
    ) == datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "names"),
    [
        ("*", "*"),
        ("1", "mon"),
        ("0", "sun"),
        ("7", "sun"),
        ("1-5", "mon,tue,wed,thu,fri"),
        ("*/2", "sun,tue,thu,sat"),
        ("5-7", "sun,fri,sat"),
        ("MON-FRI", "mon,tue,wed,thu,fri"),
        ("1,3,5", "mon,wed,fri"),
    ],
)
def test_the_weekday_field_is_read_as_standard_cron(field: str, names: str) -> None:
    # APScheduler 3.x alone reads 0 as Monday; a crontab reads it as Sunday.
    from common.utils.src.cron import standard_day_of_week

    assert standard_day_of_week(field) == names


@pytest.mark.parametrize("field", ["8", "5-1", "x", "2/2", "*/0", ""])
def test_a_weekday_field_that_does_not_parse_is_refused(field: str) -> None:
    from common.utils.src.cron import standard_day_of_week

    with pytest.raises(ValueError):
        standard_day_of_week(field)


@pytest.mark.parametrize(
    ("cron", "weekday"),
    [("0 9 * * 1", 0), ("0 9 * * 0", 6), ("0 9 * * 7", 6), ("0 9 * * 5", 4)],
)
def test_a_numbered_weekday_fires_on_that_weekday(cron: str, weekday: int) -> None:
    # Python's weekday(): Monday is 0, Sunday is 6.
    assert rules.next_slot(cron, "UTC", NOW).weekday() == weekday


@pytest.mark.parametrize(
    ("cron", "tz"), [("61 9 * * 1", "UTC"), ("0 9 * * 1", "Mars/Olympus")]
)
def test_next_slot_refuses_what_does_not_parse(cron: str, tz: str) -> None:
    with pytest.raises(ValueError):
        rules.next_slot(cron, tz, NOW)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([{"type": "done", "run_id": "r-1"}], (rules.ATTESTED_UNSETTLED, None, "r-1")),
        ([{"type": "done"}], (rules.ERROR, rules.NO_RECEIPT, None)),
        (
            [
                {
                    "type": "interrupt",
                    "interrupt_type": "HITL_APPROVAL",
                    "metadata": {"capability_id": "delete_branch"},
                },
                {"type": "done"},
            ],
            (rules.PAUSED, "awaiting approval: delete_branch (HITL_APPROVAL)", None),
        ),
        (
            [{"type": "error", "category": "step_budget", "error": "hit its budget"}],
            (rules.ERROR, "step_budget: hit its budget", None),
        ),
        (
            [{"type": "stopped"}],
            (rules.ERROR, "stopped: the run was stopped before it finished", None),
        ),
        ([], (rules.ERROR, "no_outcome: the run ended without finishing", None)),
    ],
)
def test_how_a_turn_ended_decides_the_firing_state(events, expected) -> None:
    got = rules.outcome_of(events)
    assert (got.state, got.detail, got.run_id) == expected


def test_every_state_written_is_in_the_shared_vocabulary() -> None:
    assert set(rules.IN_PROGRESS) <= set(rules.STATES)
    for events in (
        [{"type": "done", "run_id": "r"}],
        [{"type": "done"}],
        [{"type": "stopped"}],
    ):
        assert rules.outcome_of(events).state in rules.STATES


# -- a due routine fires once, through the chat runner ------------------------


async def test_a_due_routine_claims_then_records_then_runs() -> None:
    db = DB()
    _seed(db)
    sessions: list[tuple] = []
    runner = Runner(log=db.log)
    await _tick(_scheduler(db, runner, sessions=sessions))

    # claim (CAS) → row → session → running → the turn → end state, in that order
    assert db.log == [
        "routine.update_many:1",
        "firing.create:scheduled",
        "session.create",
        "firing.update:running",
        "runner.run_turn",
        "firing.update:attested_unsettled",
    ]
    routine = db.workflowtemplate.rows[0]
    assert routine["next_run_at"] == SLOT + timedelta(days=7)
    (firing,) = db.routinefiring.rows
    assert (firing["slot"], firing["office_id"], firing["user_id"]) == (
        SLOT,
        "off-1",
        "bob",
    )
    assert (firing["state"], firing["run_id"]) == (rules.ATTESTED_UNSETTLED, "run-1")

    (call,) = runner.calls
    assert call["session_id"] == firing["session_id"]
    assert call["user_id"] == "bob"  # the routine's owner, never anyone else
    assert call["user_message"] == "Summarise this week's open issues"
    assert call["model"] == "m/1"
    assert call["acceptance_criteria"] == {"citations_required": True}
    assert call["routine_context"] == {
        "routine_id": "wf-1",
        "firing_id": firing["id"],
        "connections": [DID],
        "scope_allow": [f"{DID}#create_comment"],
    }
    # the session is made in the routine's office, as its owner
    ((session_id, user_id, title, office_id),) = sessions
    assert (session_id, user_id, office_id) == (firing["session_id"], "bob", "off-1")
    assert title == "Routine: Weekly digest"


async def test_a_routine_not_yet_due_or_switched_off_does_not_fire() -> None:
    db = DB()
    _seed(db, next_run_at=NOW + timedelta(minutes=1))
    _seed(db, id="wf-2", schedule_enabled=False)
    await _tick(_scheduler(db))
    assert db.routinefiring.rows == []


async def test_two_schedulers_on_one_slot_fire_it_once() -> None:
    db = DB()
    routine = SimpleNamespace(**_seed(db))  # both read next_run_at = SLOT
    first, second = _scheduler(db), _scheduler(db)
    async with db.factory()() as conn:
        won = await first.claim(conn, routine, NOW)
        lost = await second.claim(conn, routine, NOW)
    assert won is not None and lost is None
    assert len(db.routinefiring.rows) == 1
    assert db.log.count("routine.update_many:0") == 1  # the loser saw zero rows


async def test_a_slot_that_already_has_a_row_is_never_fired_again() -> None:
    db = DB()
    routine = SimpleNamespace(**_seed(db))
    db.routinefiring.rows.append(
        {
            "routine_id": "wf-1",
            "slot": SLOT,
            "state": rules.ERROR,
            "detail": rules.RESTART,
        }
    )
    async with db.factory()() as conn:
        assert await _scheduler(db).claim(conn, routine, NOW) is None
    assert len(db.routinefiring.rows) == 1


async def test_a_crash_after_the_claim_skips_the_slot_and_never_repeats_it() -> None:
    db = DB()
    routine = SimpleNamespace(**_seed(db))
    crashed = _scheduler(db)
    async with db.factory()() as conn:
        assert await crashed.claim(conn, routine, NOW) is not None
    # ...the process dies here, before fire(). A new process starts:
    restarted = _scheduler(db)
    assert await restarted.sweep_restart() == 1
    await _tick(restarted)
    assert _firings(db) == [(SLOT, rules.ERROR, rules.RESTART)]


async def test_a_firing_in_progress_blocks_a_second_concurrent_firing() -> None:
    db = DB()
    _seed(db)
    earlier = SLOT - timedelta(days=7)
    for state in rules.IN_PROGRESS:
        db.routinefiring.rows[:] = [
            {"routine_id": "wf-1", "slot": earlier, "state": state, "detail": None}
        ]
        db.workflowtemplate.rows[0]["next_run_at"] = SLOT
        runner = Runner()
        await _tick(_scheduler(db, runner))
        assert runner.calls == [], state
        assert _firings(db)[-1] == (SLOT, rules.SKIPPED, rules.OVERLAP)
        # the slot was still claimed: next week is the next chance
        assert db.workflowtemplate.rows[0]["next_run_at"] == SLOT + timedelta(days=7)


async def test_a_schedule_that_no_longer_parses_is_turned_off_with_a_reason() -> None:
    db = DB()
    _seed(db, schedule_cron="not a cron")
    await _tick(_scheduler(db))
    routine = db.workflowtemplate.rows[0]
    assert (routine["schedule_enabled"], routine["next_run_at"]) == (False, None)
    ((slot, state, detail),) = _firings(db)
    assert (slot, state) == (SLOT, rules.ERROR)
    assert detail.startswith(rules.INVALID_SCHEDULE)


# -- a firing that cannot succeed calls nothing --------------------------------


async def _fire_refused(db: DB, **kw: Any) -> tuple[str, Runner]:
    runner = Runner()
    await _tick(_scheduler(db, runner, **kw))
    (firing,) = db.routinefiring.rows
    assert firing["state"] == rules.ERROR
    assert firing["session_id"] is None  # no session, no run
    return firing["detail"], runner


async def test_an_owner_no_longer_in_the_office_fires_nothing() -> None:
    db = DB()
    _seed(db)
    db.officemember.rows.clear()
    checked: list[str] = []

    async def check(did: str, user_id: str) -> str | None:
        checked.append(did)
        return None

    detail, runner = await _fire_refused(db, check_connection=check)
    assert detail == rules.OWNER_REMOVED
    assert runner.calls == [] and checked == []  # zero connector work


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},  # revoked (story 1.7)
        {"office_id": "off-2"},  # moved out of this office
        {"user_id": "alice"},  # someone else's
    ],
)
async def test_a_revoked_or_foreign_connection_fires_nothing(change) -> None:
    db = DB()
    _seed(db)
    db.agent.rows[0].update(change)
    detail, runner = await _fire_refused(db)
    assert detail == rules.CONNECTION_REVOKED.format(did=DID)
    assert runner.calls == []


async def test_a_connection_with_no_token_fails_closed_with_its_name() -> None:
    db = DB()
    _seed(db)

    async def missing(did: str, user_id: str) -> str | None:
        return "credential_missing: this connection needs MCP_TOKEN; add it from the connect form"

    detail, runner = await _fire_refused(db, check_connection=missing)
    assert detail.startswith("credential_missing: ")
    assert runner.calls == []


@pytest.mark.parametrize(
    ("flags", "prefix"),
    [((False, True), "connections_disabled"), ((True, False), "attestation_off")],
)
async def test_flags_off_fire_nothing(flags, prefix) -> None:
    db = DB()
    _seed(db)
    detail, runner = await _fire_refused(db, flags=lambda: flags)
    assert detail.startswith(prefix)
    assert runner.calls == []


# -- how the run ended --------------------------------------------------------


async def test_a_destructive_call_pauses_the_firing_and_the_resume_moves_it_on() -> (
    None
):
    db = DB()
    _seed(db)
    pause = {
        "type": "interrupt",
        "interrupt_type": "HITL_APPROVAL",
        "metadata": {"capability_id": "delete_branch"},
    }
    await _tick(_scheduler(db, Runner(events=[pause, {"type": "done"}])))
    (firing,) = db.routinefiring.rows
    assert firing["state"] == rules.PAUSED
    assert "delete_branch" in firing["detail"]
    assert firing["session_id"]  # where the approval card waits

    # the next slot is skipped while it waits
    db.workflowtemplate.rows[0]["next_run_at"] = NOW
    await _tick(_scheduler(db, Runner()))
    assert db.routinefiring.rows[-1]["state"] == rules.SKIPPED

    # the owner approves from the session; the turn finishes and is sealed
    await record_resume(
        firing["session_id"], [{"type": "done", "run_id": "run-9"}], db.factory()
    )
    assert (firing["state"], firing["run_id"]) == (rules.ATTESTED_UNSETTLED, "run-9")


async def test_resume_in_a_session_that_is_not_a_paused_firing_changes_nothing() -> (
    None
):
    db = DB()
    await record_resume("chat-session", [{"type": "done", "run_id": "r"}], db.factory())
    assert db.log == []


async def test_a_resume_failure_logs_the_session_id_on_one_line(caplog) -> None:
    def boom():
        raise RuntimeError("db down")

    with caplog.at_level(logging.ERROR, logger="superagent.workflow.scheduler"):
        await record_resume("sess-1\nERROR forged\r", [{"type": "done"}], boom)
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.endswith("(sess-1\\nERROR forged\\r)")


@pytest.mark.parametrize(
    ("events", "state"),
    [
        (
            [{"type": "error", "category": "transient", "error": "upstream"}],
            rules.ERROR,
        ),
        ([{"type": "done"}], rules.ERROR),
        ([RuntimeError("boom")], rules.ERROR),
    ],
)
async def test_a_failed_run_is_an_error_never_settled(events, state) -> None:
    db = DB()
    _seed(db)
    await _tick(_scheduler(db, Runner(events=events)))
    (firing,) = db.routinefiring.rows
    assert firing["state"] == state
    assert firing["run_id"] is None


# -- the gate's judgement lands on the firing row (story 2.3, AD-12) ---------


def _gate_row(db: DB, run_id: str, outcome: str, checks: list[str]) -> None:
    db.attestedsettlement.rows.append(
        {
            "run_id": run_id,
            "outcome": outcome,
            "settled_run_id": run_id if outcome == "settled" else None,
            "call_id": None,
            "failed_checks": checks,
        }
    )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], (rules.ATTESTED_UNSETTLED, None)),
        ([("settled", [])], (rules.SETTLED, None)),
        ([("refused", ["verdict_fail"])], (rules.REFUSED, "verdict_fail")),
        (
            [("refused", ["signer_did", "x"])],
            (rules.REFUSED, "signer_did, x"),
        ),
        # a replay of a settled run adds a refusal; the run stays settled
        ([("settled", []), ("refused", ["already_settled"])], (rules.SETTLED, None)),
        # a claim rolled back with its credit, then settled on retry
        ([("refused", ["credit_write_error"]), ("settled", [])], (rules.SETTLED, None)),
    ],
)
def test_the_gate_decides_how_an_attested_firing_ends(rows, expected) -> None:
    ledger = [SimpleNamespace(outcome=o, failed_checks=c) for o, c in rows]
    got = rules.judged(rules.Outcome(rules.ATTESTED_UNSETTLED, None, "r1"), ledger)
    assert (got.state, got.detail, got.run_id) == (*expected, "r1")


@pytest.mark.parametrize("state", [rules.PAUSED, rules.ERROR])
def test_the_gate_never_changes_a_firing_that_did_not_seal(state) -> None:
    outcome = rules.Outcome(state, "why")
    settled = [SimpleNamespace(outcome="settled", failed_checks=[])]
    assert rules.judged(outcome, settled) is outcome


@pytest.mark.parametrize(
    ("outcome", "checks", "state", "detail"),
    [
        ("settled", [], rules.SETTLED, None),
        ("refused", ["verdict_fail"], rules.REFUSED, "verdict_fail"),
    ],
)
async def test_a_firing_ends_as_the_gate_judged_its_run(
    outcome, checks, state, detail
) -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", outcome, checks)  # the Runner's run seals as run-1
    await _tick(_scheduler(db))
    (firing,) = db.routinefiring.rows
    assert (firing["state"], firing["detail"], firing["run_id"]) == (
        state,
        detail,
        "run-1",
    )


async def test_with_the_gate_off_a_sealed_firing_stays_attested_unsettled() -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "another-run", "settled", [])
    await _tick(_scheduler(db))
    assert db.routinefiring.rows[0]["state"] == rules.ATTESTED_UNSETTLED


async def test_a_ledger_that_cannot_be_read_leaves_the_firing_attested() -> None:
    db = DB()
    _seed(db)

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database is down")

    db.attestedsettlement.find_many = down  # type: ignore[method-assign]
    await _tick(_scheduler(db))
    (firing,) = db.routinefiring.rows
    assert (firing["state"], firing["run_id"]) == (rules.ATTESTED_UNSETTLED, "run-1")


async def test_a_resumed_firing_ends_as_the_gate_judged_its_run() -> None:
    db = DB()
    _seed(db)
    pause = {"type": "interrupt", "interrupt_type": "HITL_APPROVAL", "metadata": {}}
    await _tick(_scheduler(db, Runner(events=[pause, {"type": "done"}])))
    (firing,) = db.routinefiring.rows
    _gate_row(db, "run-9", "refused", ["verdict_fail"])
    await record_resume(
        firing["session_id"], [{"type": "done", "run_id": "run-9"}], db.factory()
    )
    assert (firing["state"], firing["detail"]) == (rules.REFUSED, "verdict_fail")


async def test_without_a_runner_the_scheduler_claims_nothing() -> None:
    db = DB()
    _seed(db)
    scheduler = WorkflowScheduler(db=db.factory(), clock=lambda: NOW)
    assert await scheduler._tick() == []
    assert db.log == []


# -- the seams: run_turn carries the bounds; a resume moves the row on --------


class _CapturingGraph:
    def __init__(self, snapshots: list[Any]) -> None:
        self._snapshots = list(snapshots)
        self.inputs: list[Any] = []

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return self._snapshots.pop(0) if self._snapshots else None

    def astream(self, stream_input: Any, config: dict[str, Any], **_: Any) -> Any:
        self.inputs.append(stream_input)

        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()


async def test_run_turn_puts_the_routine_bounds_in_state_for_that_turn_only(
    monkeypatch,
) -> None:
    from langchain_core.messages import HumanMessage
    from superagent.graph.runner import SessionRunner

    async def _noop(self, *args, **kwargs):
        return None

    monkeypatch.setattr(SessionRunner, "_persist_transcript", _noop)
    had_messages = SimpleNamespace(
        values={"messages": [HumanMessage(content="hi")]}, tasks=[]
    )
    graph = _CapturingGraph([had_messages, None, had_messages, None])
    bounds = {
        "routine_id": "wf-1",
        "firing_id": "f-1",
        "connections": [DID],
        "scope_allow": [],
    }
    runner = SessionRunner(graph)
    async for _ in runner.run_turn("s-1", "bob", "goal", routine_context=bounds):
        pass
    async for _ in runner.run_turn("s-1", "bob", "a chat turn after"):
        pass
    firing_turn, chat_turn = graph.inputs
    assert firing_turn["routine_context"] == bounds
    assert chat_turn["routine_context"] is None  # a later chat turn is a chat turn


def test_the_resume_route_hands_its_end_events_to_the_firing_row(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from superagent.api import routes as api_routes

    class _Runner:
        async def resume_from_interrupt(self, **_: Any):
            yield {"type": "token", "content": "x"}
            yield {"type": "done", "session_id": "s-9", "run_id": "run-9"}

    recorded = AsyncMock()
    monkeypatch.setattr("superagent.workflow.scheduler.record_resume", recorded)
    app = FastAPI()
    app.include_router(api_routes.router)
    app.state.runner = _Runner()
    with TestClient(app) as client:
        resp = client.post(
            "/sessions/s-9/resume",
            json={"user_id": "bob", "value": {"status": "approved"}},
        )
        assert resp.status_code == 200
        assert "run-9" in resp.text
    recorded.assert_awaited_once_with(
        "s-9", [{"type": "done", "session_id": "s-9", "run_id": "run-9"}]
    )
