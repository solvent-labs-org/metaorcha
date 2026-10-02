"""Story 2.2: turning a routine's schedule on, and reading its firings.

``PATCH /api/v1/workflows/{id}`` with ``status: scheduled`` turns the
schedule on at the next slot after now (never a catch-up); ``inactive``
turns it off. Only the routine's own member turns it on. The routines list
and ``GET /api/v1/workflows/{id}/firings`` show how firings ended (AD-22),
with the session a paused firing's approval waits in — visible exactly as
the routine is (story 2.0).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from .office_db import FakeDB

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

OFFICE = "off-1"


class _Redis:
    async def sismember(self, _key: str, _member: str) -> bool:
        return False


def _routine(db: FakeDB, rid: str, user_id: str, **extra) -> None:
    db.workflowtemplate.insert(
        id=rid,
        user_id=user_id,
        office_id=OFFICE,
        name=rid,
        description=None,
        goal_template="g",
        agents_used=[],
        steps=[],
        parameters={},
        schedule_cron="0 9 * * 1",
        schedule_tz="Europe/London",
        **extra,
    )


def _firing(db: FakeDB, rid: str, slot: datetime, state: str, **extra) -> None:
    db.routinefiring.insert(
        routine_id=rid, office_id=OFFICE, user_id="bob", slot=slot, state=state, **extra
    )


@pytest_asyncio.fixture
async def env(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    monkeypatch.setattr(settings, "connections_enabled", True)
    db = FakeDB()
    db.add_office(OFFICE, {"alice": "OWNER", "bob": "MEMBER", "carol": "MEMBER"})
    _routine(db, "wf-b", "bob")
    _routine(db, "wf-c", "carol")
    app.state.db = db
    app.state.redis = _Redis()
    headers = {
        uid: {
            "Authorization": "Bearer "
            + create_access_token(user_id=uid, email=f"{uid}@example.com")[0],
            "X-Orcha-Office": OFFICE,
        }
        for uid in ("alice", "bob", "carol")
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, db, headers


# ── the schedule ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turning_the_schedule_on_sets_the_next_slot_after_now(env) -> None:
    ac, db, h = env
    before = datetime.now(UTC)
    resp = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    assert resp.status_code == 200, resp.text
    row = db.workflowtemplate.get("wf-b")
    assert row.schedule_enabled is True and row.status == "scheduled"
    nxt = row.next_run_at
    assert before < nxt <= before + timedelta(days=7, hours=1)
    # Monday 09:00 in London, read as a standard crontab (1 = Monday)
    from zoneinfo import ZoneInfo

    local = nxt.astimezone(ZoneInfo("Europe/London"))
    assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)
    assert resp.json()["next_run_at"] is not None


@pytest.mark.asyncio
async def test_pausing_turns_the_schedule_off(env) -> None:
    ac, db, h = env
    await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    resp = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "inactive"}, headers=h["bob"]
    )
    assert resp.status_code == 200
    row = db.workflowtemplate.get("wf-b")
    assert (row.schedule_enabled, row.next_run_at, row.status) == (
        False,
        None,
        "inactive",
    )


@pytest.mark.asyncio
async def test_an_owner_may_pause_a_members_schedule_but_not_turn_it_on(env) -> None:
    ac, db, h = env
    on = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["alice"]
    )
    assert on.status_code == 403
    assert db.workflowtemplate.get("wf-b").schedule_enabled is False
    await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    off = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "inactive"}, headers=h["alice"]
    )
    assert off.status_code == 200
    assert db.workflowtemplate.get("wf-b").schedule_enabled is False


@pytest.mark.asyncio
async def test_a_template_with_no_schedule_cannot_be_turned_on(env) -> None:
    ac, db, h = env
    for row in db.workflowtemplate.rows:
        if row["id"] == "wf-b":
            row["schedule_cron"] = None
    resp = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "status"


@pytest.mark.asyncio
async def test_with_connections_off_a_schedule_cannot_be_turned_on(
    env, monkeypatch
) -> None:
    from gateway.config import settings

    ac, db, h = env
    monkeypatch.setattr(settings, "connections_enabled", False)
    resp = await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    assert resp.status_code == 403
    assert db.workflowtemplate.get("wf-b").schedule_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cron", "tz"),
    [("0 9 * * 1", "UTC"), ("0 9 * * 1-5", "UTC"), ("30 8 * * 0", "Asia/Karachi")],
)
async def test_save_and_fire_read_the_weekday_the_same_way(env, cron, tz) -> None:
    # The Gateway's first slot and the SuperAgent's later slots go through the
    # one translation; 1 is Monday, 0 Sunday, as in any crontab.
    from common.utils.src.cron import next_slot

    ac, db, h = env
    for row in db.workflowtemplate.rows:
        if row["id"] == "wf-b":
            row.update(schedule_cron=cron, schedule_tz=tz)
    await ac.patch(
        "/api/v1/workflows/wf-b", json={"status": "scheduled"}, headers=h["bob"]
    )
    first = db.workflowtemplate.get("wf-b").next_run_at
    assert first == next_slot(cron, tz, first - timedelta(seconds=1))


# ── firings ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_list_shows_each_routines_newest_firing(env) -> None:
    ac, db, h = env
    week = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    _firing(db, "wf-b", week, "error", detail="credential_missing: needs MCP_TOKEN")
    _firing(
        db,
        "wf-b",
        week + timedelta(days=7),
        "paused",
        detail="awaiting approval: delete_branch (HITL_APPROVAL)",
        session_id="s-paused",
    )
    resp = await ac.get("/api/v1/workflows", headers=h["bob"])
    (only,) = resp.json()
    assert only["id"] == "wf-b"
    last = only["last_firing"]
    assert (last["state"], last["session_id"]) == ("paused", "s-paused")


@pytest.mark.asyncio
async def test_firings_are_newest_first_and_bounded(env) -> None:
    ac, db, h = env
    base = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    for week in range(5):
        _firing(
            db,
            "wf-b",
            base + timedelta(days=7 * week),
            "attested_unsettled",
            run_id=f"r{week}",
        )
    resp = await ac.get("/api/v1/workflows/wf-b/firings?limit=3", headers=h["bob"])
    assert resp.status_code == 200
    assert [f["run_id"] for f in resp.json()] == ["r4", "r3", "r2"]


@pytest.mark.asyncio
async def test_firings_are_visible_exactly_as_the_routine_is(env) -> None:
    ac, db, h = env
    _firing(db, "wf-c", datetime(2026, 10, 5, 8, 0, tzinfo=UTC), "error")
    # a member: not a colleague's
    assert (
        await ac.get("/api/v1/workflows/wf-c/firings", headers=h["bob"])
    ).status_code == 404
    # the owner: any routine in the office
    resp = await ac.get("/api/v1/workflows/wf-c/firings", headers=h["alice"])
    assert [f["state"] for f in resp.json()] == ["error"]
    # nobody, from another office
    other = {**h["carol"], "X-Orcha-Office": "po_carol"}
    assert (
        await ac.get("/api/v1/workflows/wf-c/firings", headers=other)
    ).status_code == 404
