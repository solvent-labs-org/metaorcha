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
import sys
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
        if key == "NOT":  # Prisma: the row matches none of these
            if any(_matches(row, clause) for clause in want):
                return False
            continue
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
        assert order in (None, {"created_at": "asc"}, {"created_at": "desc"})
        found = [SimpleNamespace(**r) for r in self.rows if _matches(r, where)]
        return found[::-1] if order == {"created_at": "desc"} else found

    async def find_first(
        self, where: dict[str, Any], order: Any = None
    ) -> SimpleNamespace | None:
        found = await self.find_many(where, order)
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
        gate_on=kw.pop("gate_on", lambda: True),
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
        "criteria_operands": {},
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
    ("rows", "failing", "expected"),
    [
        ([], (), (rules.ATTESTED_UNSETTLED, None)),
        ([], ("counts_match",), (rules.ATTESTED_UNSETTLED, None)),
        ([("settled", [])], (), (rules.SETTLED, None)),
        ([("settled", [])], ("counts_match",), (rules.SETTLED, None)),
        ([("refused", ["verdict_fail"])], (), (rules.REFUSED, "verdict_fail")),
        # story 2.5: the gate's verdict_fail is named by the signed verdicts
        (
            [("refused", ["verdict_fail"])],
            ("counts_match",),
            (rules.REFUSED, "counts_match"),
        ),
        (
            [("refused", ["verdict_fail", "signature"])],
            ("structural_verification", "declared_acceptance"),
            (rules.REFUSED, "structural_verification, declared_acceptance, signature"),
        ),
        (
            [("refused", ["signer_did", "x"])],
            ("counts_match",),
            (rules.REFUSED, "signer_did, x"),
        ),
        (
            [("refused", ["signer_did", "x"])],
            (),
            (rules.REFUSED, "signer_did, x"),
        ),
        ([("refused", [])], (), (rules.REFUSED, None)),
        # a replay of a settled run adds a refusal; the run stays settled
        (
            [("settled", []), ("refused", ["already_settled"])],
            (),
            (rules.SETTLED, None),
        ),
        # a claim rolled back with its credit, then settled on retry
        (
            [("refused", ["credit_write_error"]), ("settled", [])],
            (),
            (rules.SETTLED, None),
        ),
        # two refusals: the oldest decides
        (
            [("refused", ["verdict_fail"]), ("refused", ["already_settled"])],
            ("counts_match",),
            (rules.REFUSED, "counts_match"),
        ),
    ],
)
def test_the_gate_decides_how_an_attested_firing_ends(rows, failing, expected) -> None:
    ledger = [SimpleNamespace(outcome=o, failed_checks=c) for o, c in rows]
    got = rules.judged(
        rules.Outcome(rules.ATTESTED_UNSETTLED, None, "r1"), ledger, failing
    )
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


# -- a refused firing names its check (story 2.5) ------------------------------

PASS = {"check": "structural_verification", "result": "pass", "detail": "c1: ok"}
COUNTS_FAIL = {
    "check": "counts_match",
    "result": "fail",
    "detail": "left=11 right=10 key=open_issues",
}


def _seal(
    db: DB, session_id: str | None, run_id: str | None, *verdicts: dict[str, Any]
) -> None:
    """A stored attestation: a run envelope, or a case attestation (run_id None)."""
    db.attestation.rows.append(
        {
            "session_id": session_id,
            "run_id": run_id,
            "payload": {"run_id": run_id, "verdicts": list(verdicts or [PASS])},
        }
    )


def _end(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (row["state"], row["detail"], row["run_id"])


async def test_a_refused_firing_names_the_failing_verdict_from_its_envelope() -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "refused", ["verdict_fail"])
    _seal(db, "elsewhere", "run-1", PASS, COUNTS_FAIL)  # read by run_id
    await _tick(_scheduler(db))
    (firing,) = db.routinefiring.rows
    assert _end(firing) == (rules.REFUSED, "counts_match", "run-1")
    # the ledger keeps the gate's own id
    assert db.attestedsettlement.rows[0]["failed_checks"] == ["verdict_fail"]


async def test_an_envelope_with_no_failing_verdict_keeps_the_gates_id() -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "refused", ["verdict_fail"])
    _seal(db, "elsewhere", "run-1", PASS)
    await _tick(_scheduler(db))
    assert _end(db.routinefiring.rows[0]) == (rules.REFUSED, "verdict_fail", "run-1")


async def test_a_validator_import_failure_still_records_the_judgement(
    monkeypatch,
) -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "refused", ["verdict_fail"])
    _seal(db, "elsewhere", "run-1", COUNTS_FAIL)
    monkeypatch.setitem(sys.modules, "validator.run_envelope", None)
    await _tick(_scheduler(db))
    # the names are lost, never the judgement
    assert _end(db.routinefiring.rows[0]) == (rules.REFUSED, "verdict_fail", "run-1")


async def test_a_refusal_on_the_gates_own_checks_reads_no_envelope() -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "refused", ["signature"])

    async def unread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no envelope read without verdict_fail")

    db.attestation.find_unique = unread  # type: ignore[method-assign]
    await _tick(_scheduler(db))
    assert _end(db.routinefiring.rows[0]) == (rules.REFUSED, "signature", "run-1")


# -- after a restart (AD-22, story 2.5) ----------------------------------------


def _firing(
    db: DB,
    state: str,
    session_id: str | None = "s-1",
    run_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """A firing row the previous process left behind."""
    row = {
        "id": f"firing-{next(_ids)}",
        "routine_id": "wf-1",
        "slot": SLOT - timedelta(days=7),
        "state": state,
        "detail": detail,
        "run_id": run_id,
        "session_id": session_id,
    }
    db.routinefiring.rows.append(row)
    return row


async def test_a_running_firing_with_no_sealed_envelope_is_error_restart() -> None:
    db = DB()
    row = _firing(db, rules.RUNNING)
    assert await _scheduler(db).sweep_restart() == 1
    assert _end(row) == (rules.ERROR, rules.RESTART, None)


@pytest.mark.parametrize("state", [rules.RUNNING, rules.PAUSED])
@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        (("settled", []), (rules.SETTLED, None)),
        (None, (rules.ATTESTED_UNSETTLED, None)),
        (("refused", ["verdict_fail"]), (rules.REFUSED, "counts_match")),
    ],
)
async def test_a_firing_whose_run_sealed_keeps_its_receipt_and_judgement(
    state, gate, expected
) -> None:
    db = DB()
    row = _firing(
        db, state, detail="awaiting approval: x" if state == rules.PAUSED else None
    )
    refused = gate is not None and gate[0] == "refused"
    _seal(db, "s-1", "run-7", *([COUNTS_FAIL] if refused else [PASS]))
    if gate is not None:
        _gate_row(db, "run-7", *gate)
    assert await _scheduler(db).sweep_restart() == 1
    assert _end(row) == (*expected, "run-7")


async def test_the_newest_seal_in_the_session_is_the_firings_run() -> None:
    db = DB()
    row = _firing(db, rules.RUNNING)
    _seal(db, "s-1", "run-a")
    _seal(db, "s-1", None)  # a case attestation after it is not a seal
    _seal(db, "s-1", "run-b")
    _seal(db, "s-1", None)
    await _scheduler(db).sweep_restart()
    assert _end(row) == (rules.ATTESTED_UNSETTLED, None, "run-b")


async def test_a_case_attestation_is_not_a_seal() -> None:
    db = DB()
    running = _firing(db, rules.RUNNING, "s-1")
    paused = _firing(db, rules.PAUSED, "s-2", detail="awaiting approval: x")
    _seal(db, "s-1", None)
    _seal(db, "s-2", None)
    await _scheduler(db).sweep_restart()
    assert _end(running) == (rules.ERROR, rules.RESTART, None)
    assert _end(paused) == (rules.PAUSED, "awaiting approval: x", None)


async def test_the_sweep_leaves_unsealed_paused_and_finished_rows_alone() -> None:
    db = DB()
    _firing(db, rules.PAUSED, "s-p", detail="awaiting approval: delete_branch")
    _firing(db, rules.SETTLED, "s-s", run_id="run-s")
    _firing(db, rules.REFUSED, "s-f", run_id="run-f", detail="counts_match")
    _firing(db, rules.ERROR, "s-e", detail=rules.INTERNAL)
    _firing(db, rules.SKIPPED, None, detail=rules.OVERLAP)
    _firing(db, rules.RUNNING, "s-r", run_id="run-r")  # its run_id is written
    _firing(db, rules.ATTESTED_UNSETTLED, "s-u", run_id="run-u")  # no gate row
    for session, run in (("s-s", "run-s"), ("s-f", "run-f"), ("s-r", "run-r")):
        _seal(db, session, run)
    _gate_row(db, "run-other", "settled", [])
    before = [dict(r) for r in db.routinefiring.rows]
    assert await _scheduler(db).sweep_restart() == 0
    assert db.routinefiring.rows == before


async def test_the_sweep_reconciles_an_attested_unsettled_row_with_a_gate_row() -> None:
    db = DB()
    refused = _firing(db, rules.ATTESTED_UNSETTLED, "s-1", run_id="r1")
    settled = _firing(db, rules.ATTESTED_UNSETTLED, "s-2", run_id="r2")
    no_row = _firing(db, rules.ATTESTED_UNSETTLED, "s-3", run_id="r3")
    _seal(db, "s-1", "r1", COUNTS_FAIL)
    _gate_row(db, "r1", "refused", ["verdict_fail"])
    _gate_row(db, "r2", "settled", [])
    assert await _scheduler(db).sweep_restart() == 2
    assert _end(refused) == (rules.REFUSED, "counts_match", "r1")
    assert _end(settled) == (rules.SETTLED, None, "r2")
    # no gate decision is recorded for r3: it says so, still
    assert _end(no_row) == (rules.ATTESTED_UNSETTLED, None, "r3")


async def test_a_reconcile_failure_never_undoes_the_sweep() -> None:
    db = DB()
    running = _firing(db, rules.RUNNING, "s-1")
    unsettled = _firing(db, rules.ATTESTED_UNSETTLED, "s-2", run_id="r2")
    _gate_row(db, "r2", "settled", [])
    real = db.attestedsettlement.find_many

    async def down(where: dict[str, Any], order: Any = None) -> Any:
        if isinstance(where.get("run_id"), dict):  # the reconcile's batched read
            raise RuntimeError("database is down")
        return await real(where, order)

    db.attestedsettlement.find_many = down  # type: ignore[method-assign]
    scheduler = _scheduler(db)
    assert await scheduler.sweep_restart() == 1
    assert _end(running) == (rules.ERROR, rules.RESTART, None)
    assert _end(unsettled) == (rules.ATTESTED_UNSETTLED, None, "r2")
    assert scheduler._sweep_pending is False


async def test_the_sweep_never_overwrites_a_row_that_moved() -> None:
    db = DB()
    row = _firing(db, rules.RUNNING, "s-1")
    _seal(db, "s-1", "run-1")
    real = db.attestation.find_first

    async def moved(where: dict[str, Any], order: Any = None) -> Any:
        # the firing's own last write lands between the sweep's read and write
        row.update(state=rules.SETTLED, run_id="run-1")
        return await real(where, order)

    db.attestation.find_first = moved  # type: ignore[method-assign]
    assert await _scheduler(db).sweep_restart() == 0
    assert _end(row) == (rules.SETTLED, None, "run-1")
    assert db.log[-1] == "firing.update_many:0"


async def test_one_failing_row_does_not_abort_the_sweep() -> None:
    db = DB()
    bad = _firing(db, rules.RUNNING, "s-bad")
    ok = _firing(db, rules.RUNNING, "s-ok")
    real = db.attestation.find_first
    fault = {"on": True}

    async def flaky(where: dict[str, Any], order: Any = None) -> Any:
        if fault["on"] and where.get("session_id") == "s-bad":
            raise RuntimeError("connection reset")
        return await real(where, order)

    db.attestation.find_first = flaky  # type: ignore[method-assign]
    scheduler = _scheduler(db)
    await scheduler._poll()
    assert _end(ok) == (rules.ERROR, rules.RESTART, None)
    assert _end(bad) == (rules.RUNNING, None, None)
    assert scheduler._unswept == {bad["id"]}
    assert scheduler._sweep_pending is False  # the sweep completed; slots claim

    # The next pass retries only that id: never a full sweep, which could
    # meet this process's own running firings.
    wheres: list[dict[str, Any]] = []
    real_firings = db.routinefiring.find_many

    async def seen(where: dict[str, Any], order: Any = None) -> Any:
        wheres.append(where)
        return await real_firings(where, order)

    db.routinefiring.find_many = seen  # type: ignore[method-assign]
    fault["on"] = False
    mark = len(db.log)
    await scheduler._poll()
    assert wheres == [
        {
            "state": {"in": [rules.RUNNING, rules.PAUSED]},
            "run_id": None,
            "id": {"in": [bad["id"]]},
        }
    ]
    assert db.log[mark:] == ["firing.update_many:1"]  # no scheduled-row sweep
    assert _end(bad) == (rules.ERROR, rules.RESTART, None)
    assert scheduler._unswept == set()


async def test_a_retried_id_that_moved_on_is_dropped() -> None:
    db = DB()
    row = _firing(db, rules.RUNNING, "s-1")
    scheduler = _scheduler(db)
    scheduler._sweep_pending = False
    scheduler._unswept = {row["id"]}
    row.update(state=rules.SETTLED, run_id="run-1")  # its owner's resume landed
    await scheduler._poll()
    assert scheduler._unswept == set()
    assert _end(row) == (rules.SETTLED, None, "run-1")


async def test_a_failed_sweep_claims_nothing_until_it_completes() -> None:
    db = DB()
    _seed(db)
    real = db.routinefiring.find_many
    calls = {"n": 0}

    async def first_fails(where: dict[str, Any], order: Any = None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is down")
        return await real(where, order)

    db.routinefiring.find_many = first_fails  # type: ignore[method-assign]
    scheduler = _scheduler(db)
    with pytest.raises(RuntimeError):
        await scheduler._poll()  # the loop logs it and polls again
    assert scheduler._sweep_pending is True
    assert not [e for e in db.log if e.startswith(("firing.create", "routine."))]
    assert db.workflowtemplate.rows[0]["next_run_at"] == SLOT  # not claimed

    await scheduler._poll()
    await asyncio.gather(*list(scheduler._firings))
    assert scheduler._sweep_pending is False
    assert db.log.count("firing.create:scheduled") == 1
    (firing,) = db.routinefiring.rows
    assert _end(firing) == (rules.ATTESTED_UNSETTLED, None, "run-1")


async def test_the_restart_sweep_runs_before_the_first_claim() -> None:
    db = DB()
    _seed(db)
    # the previous process's firing of this routine: left running, it would
    # block the slot as overlapping
    stale = _firing(db, rules.RUNNING, "s-old")
    scheduler = _scheduler(db)
    await scheduler.start()
    try:
        for _ in range(200):
            if db.routinefiring.rows[-1]["state"] == rules.ATTESTED_UNSETTLED:
                break
            await asyncio.sleep(0)
    finally:
        await scheduler.stop()
    assert db.log.index("firing.update_many:1") < db.log.index(
        "firing.create:scheduled"
    )
    assert _end(stale) == (rules.ERROR, rules.RESTART, None)
    fired = db.routinefiring.rows[-1]
    assert _end(fired) == (rules.ATTESTED_UNSETTLED, None, "run-1")  # not skipped


class SealingRunner(Runner):
    """Seals ``run-1`` in the firing's session, as the run observer would."""

    def __init__(self, db: DB) -> None:
        super().__init__(log=db.log)
        self.db = db

    async def run_turn(self, **kwargs: Any):
        _seal(self.db, kwargs["session_id"], "run-1")
        async for event in super().run_turn(**kwargs):
            yield event


def _final_write_fails_once(scheduler: WorkflowScheduler) -> None:
    real = scheduler._set
    failed: list[dict[str, Any]] = []

    async def _set(firing_id: str, **data: Any) -> None:
        if "run_id" in data and not failed:  # the end-state write only
            failed.append(data)
            raise RuntimeError("connection reset")
        await real(firing_id, **data)

    scheduler._set = _set  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        (("settled", []), (rules.SETTLED, None, "run-1")),
        (None, (rules.ATTESTED_UNSETTLED, None, "run-1")),
    ],
)
async def test_a_failed_final_write_after_a_seal_records_the_judgement_not_internal(
    gate, expected
) -> None:
    db = DB()
    _seed(db)
    if gate is not None:
        _gate_row(db, "run-1", *gate)
    scheduler = _scheduler(db, SealingRunner(db))
    _final_write_fails_once(scheduler)
    await _tick(scheduler)
    (firing,) = db.routinefiring.rows
    assert _end(firing) == expected


async def test_a_seal_lookup_failing_on_recovery_is_error_internal() -> None:
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "settled", [])

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database is down")

    db.attestation.find_first = down  # type: ignore[method-assign]
    scheduler = _scheduler(db, SealingRunner(db))
    _final_write_fails_once(scheduler)
    await _tick(scheduler)
    (firing,) = db.routinefiring.rows
    assert _end(firing) == (rules.ERROR, rules.INTERNAL, None)


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


# -- review fixes (2.5): gate off, a moved row, a resume racing the sweep ------


async def test_with_the_gate_off_the_sweep_never_scans_for_a_reconcile() -> None:
    """No gate row is written with the gate off: nothing can be reconciled."""
    db = DB()
    row = _firing(db, rules.ATTESTED_UNSETTLED, "s-1", run_id="r1")
    _gate_row(db, "r1", "settled", [])  # e.g. written while the flag was on
    reads: list[Any] = []
    real = db.attestedsettlement.find_many

    async def seen(where: dict[str, Any], order: Any = None) -> Any:
        reads.append(where)
        return await real(where, order)

    db.attestedsettlement.find_many = seen  # type: ignore[method-assign]
    assert await _scheduler(db, gate_on=lambda: False).sweep_restart() == 0
    assert reads == []
    assert _end(row) == (rules.ATTESTED_UNSETTLED, None, "r1")


async def test_the_sweep_never_moves_a_row_whose_state_changed_with_no_run_id() -> None:
    """The compare-and-set guards the state, not only run_id: a running row the
    owner's resume moved to paused (run_id still NULL) is left as it is."""
    db = DB()
    row = _firing(db, rules.RUNNING, "s-1")
    real = db.attestation.find_first

    async def moved(where: dict[str, Any], order: Any = None) -> Any:
        row.update(state=rules.PAUSED, detail="awaiting approval: x (approval)")
        return await real(where, order)

    db.attestation.find_first = moved  # type: ignore[method-assign]
    assert await _scheduler(db).sweep_restart() == 0
    assert _end(row) == (rules.PAUSED, "awaiting approval: x (approval)", None)


@pytest.mark.parametrize(
    ("outcome", "checks", "expected"),
    [
        ("settled", [], (rules.SETTLED, None, "r1")),
        ("refused", ["verdict_fail"], (rules.REFUSED, "counts_match", "r1")),
    ],
)
async def test_a_resume_that_raced_the_sweep_still_takes_its_gate_judgement(
    outcome: str, checks: list[str], expected: tuple[Any, ...]
) -> None:
    """The sweep took the resume's seal before the gate wrote its row: the
    resume's own end re-judges that row, by run_id."""
    db = DB()
    row = _firing(db, rules.PAUSED, "s-1")
    _seal(db, "s-1", "r1", COUNTS_FAIL)
    await _scheduler(db).sweep_restart()
    assert _end(row) == (rules.ATTESTED_UNSETTLED, None, "r1")

    _gate_row(db, "r1", outcome, checks)  # the gate's row lands after the sweep
    await record_resume("s-1", [{"type": "done", "run_id": "r1"}], db=db.factory())
    assert _end(row) == expected


async def test_a_resume_never_rejudges_an_unsettled_row_of_another_run() -> None:
    db = DB()
    row = _firing(db, rules.ATTESTED_UNSETTLED, "s-1", run_id="r1")
    _gate_row(db, "r2", "settled", [])
    await record_resume("s-1", [{"type": "done", "run_id": "r2"}], db=db.factory())
    assert _end(row) == (rules.ATTESTED_UNSETTLED, None, "r1")


async def test_an_envelope_that_failed_verification_names_no_verdict() -> None:
    """Refused for its signature too: the envelope's verdicts are not read."""
    db = DB()
    _seed(db)
    _gate_row(db, "run-1", "refused", ["signature", "verdict_fail"])
    _seal(db, "elsewhere", "run-1", COUNTS_FAIL)
    await _tick(_scheduler(db))
    (firing,) = db.routinefiring.rows
    assert _end(firing) == (rules.REFUSED, "signature, verdict_fail", "run-1")
