"""Story 2.2: turning a routine's schedule on, and reading its firings.

``PATCH /api/v1/workflows/{id}`` with ``status: scheduled`` turns the
schedule on at the next slot after now (never a catch-up); ``inactive``
turns it off. Only the routine's own member turns it on. The routines list
and ``GET /api/v1/workflows/{id}/firings`` show how firings ended (AD-22),
with the session a paused firing's approval waits in — visible exactly as
the routine is (story 2.0).

Story 2.5: each firing carries the words the pane shows, computed here from
the row, the settlement ledger and the stored envelope — and degrading to
nothing, never to a claim, when either read fails.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from .office_db import FakeDB, fail_with

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


# ── the pane's fields (story 2.5) ────────────────────────────────────────

SLOT = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
# The validator's detail on a counts_match fail: envelope content, never the pane's.
COUNTS_FAIL = {
    "check": "counts_match",
    "result": "fail",
    "detail": "left=11 right=10 (did:orcha:agent:a#x /n, did:orcha:agent:b#y /n)",
}
FORBIDDEN = re.compile(r"\b(done|success|verified)\b", re.IGNORECASE)


def _criteria(db: FakeDB, rid: str, criteria: dict) -> None:
    for row in db.workflowtemplate.rows:
        if row["id"] == rid:
            row["criteria"] = criteria


def _envelope(db: FakeDB, run_id: str, session_id: str, verdicts: list) -> None:
    db.attestation.insert(
        run_id=run_id,
        session_id=session_id,
        case_hash="h",
        payload={"run_id": run_id, "verdicts": verdicts},
        signature="sig",
        public_key="pk",
    )


def _ledger(db: FakeDB, run_id: str, outcome: str, checks: list, **extra) -> None:
    db.attestedsettlement.insert(
        run_id=run_id,
        outcome=outcome,
        failed_checks=checks,
        envelope_digest="d",
        **extra,
    )


def _refused_counts_match(db: FakeDB) -> None:
    """bob's routine declares counts_match; its firing was refused on it."""
    _criteria(db, "wf-b", {"counts_match": True})
    _firing(
        db, "wf-b", SLOT, "refused", detail="counts_match", run_id="r1", session_id="s1"
    )
    _envelope(db, "r1", "s1", [COUNTS_FAIL])
    _ledger(db, "r1", "refused", ["verdict_fail"], session_id="s1")


async def _last(ac, headers, rid: str = "wf-b") -> dict:
    resp = await ac.get("/api/v1/workflows", headers=headers)
    assert resp.status_code == 200, resp.text
    (wf,) = [w for w in resp.json() if w["id"] == rid]
    return wf["last_firing"]


@pytest.mark.asyncio
async def test_the_pane_fields_come_from_the_row_ledger_and_envelope(env) -> None:
    ac, db, h = env
    _refused_counts_match(db)
    for path in ("/api/v1/workflows", "/api/v1/workflows/wf-b"):
        resp = await ac.get(path, headers=h["bob"])
        body = resp.json()
        last = (body[0] if isinstance(body, list) else body)["last_firing"]
        assert last["label"] == "refused — counts_match"
        assert (last["gate"], last["gate_label"], last["gate_checks"]) == (
            "verdict_only",
            "verdict only, nothing charged",
            ["verdict_fail"],
        )
        assert (last["checks"], last["checks_label"]) == (
            "checked",
            "checked: counts_match",
        )
        assert last["receipt_available"] is True
        assert last["receipt_downloadable"] is True
        assert last["note"] is None
        assert "left=11" not in resp.text  # envelope detail stays off the pane
    (firing,) = (
        await ac.get("/api/v1/workflows/wf-b/firings", headers=h["bob"])
    ).json()
    assert firing["label"] == "refused — counts_match"
    assert firing["checks_label"] == "checked: counts_match"


@pytest.mark.asyncio
async def test_the_receipt_link_needs_a_stored_envelope_in_the_firings_session(
    env,
) -> None:
    ac, db, h = env
    _firing(db, "wf-b", SLOT, "attested_unsettled", run_id="r1", session_id="s1")
    last = await _last(ac, h["bob"])
    # run_id is published before the envelope is stored: not a receipt yet
    assert (last["receipt_available"], last["receipt_downloadable"]) == (False, False)
    assert (last["checks"], last["checks_label"]) == (None, None)
    # an envelope for that run_id, stored in another session, is not this one's
    _envelope(db, "r1", "s-other", [])
    last = await _last(ac, h["bob"])
    assert (last["receipt_available"], last["receipt_downloadable"]) == (False, False)
    assert last["checks"] is None
    # ... even when a batch read returns it because another firing owns that
    # session: an envelope is matched to its firing by run_id and session_id
    _firing(
        db,
        "wf-b",
        SLOT - timedelta(days=7),
        "attested_unsettled",
        run_id="r0",
        session_id="s-other",
    )
    firings = (await ac.get("/api/v1/workflows/wf-b/firings", headers=h["bob"])).json()
    assert [(f["run_id"], f["receipt_available"]) for f in firings] == [
        ("r1", False),
        ("r0", False),
    ]


@pytest.mark.asyncio
async def test_an_owner_sees_a_members_receipt_exists_but_gets_no_link(env) -> None:
    ac, db, h = env
    _refused_counts_match(db)
    resp = await ac.get("/api/v1/workflows", headers=h["alice"])
    (wf,) = [w for w in resp.json() if w["id"] == "wf-b"]
    last = wf["last_firing"]
    assert last["receipt_available"] is True
    assert last["receipt_downloadable"] is False  # the receipt is bob's session's
    assert last["label"] == "refused — counts_match"
    assert "left=11" not in resp.text
    firings = await ac.get("/api/v1/workflows/wf-b/firings", headers=h["alice"])
    assert [f["receipt_downloadable"] for f in firings.json()] == [False]
    assert "left=11" not in firings.text


@pytest.mark.asyncio
async def test_a_ledger_read_failure_degrades_never_claims(env) -> None:
    ac, db, h = env
    _refused_counts_match(db)
    _firing(
        db,
        "wf-b",
        SLOT + timedelta(days=7),
        "attested_unsettled",
        run_id="r2",
        session_id="s2",
    )
    _envelope(db, "r2", "s2", [])
    url = "/api/v1/workflows/wf-b/firings"
    healthy = {f["run_id"]: f for f in (await ac.get(url, headers=h["bob"])).json()}
    assert healthy["r2"]["note"] == "no settlement decision is recorded for this run"
    assert healthy["r1"]["gate"] == "verdict_only"

    undo = fail_with(db.attestedsettlement, "find_many")
    resp = await ac.get(url, headers=h["bob"])
    undo()
    assert resp.status_code == 200, resp.text
    by_run = {f["run_id"]: f for f in resp.json()}
    refused, unsettled = by_run["r1"], by_run["r2"]
    assert refused["label"] == "refused — counts_match"  # from the row
    assert (refused["gate"], refused["gate_label"], refused["gate_checks"]) == (
        None,
        None,
        [],
    )
    assert unsettled["label"] == "attested but unsettled"
    assert unsettled["note"] is None  # a failed read proves no absence
    # the envelope read was unaffected
    assert refused["receipt_available"] is True
    assert refused["checks_label"] == "checked: counts_match"
    assert unsettled["receipt_available"] is True


@pytest.mark.asyncio
async def test_an_envelope_read_failure_degrades_never_claims(env) -> None:
    ac, db, h = env
    _refused_counts_match(db)
    undo = fail_with(db.attestation, "find_many")
    last = await _last(ac, h["bob"])
    undo()
    assert last["label"] == "refused — counts_match"
    assert (last["receipt_available"], last["receipt_downloadable"]) == (False, False)
    assert (last["checks"], last["checks_label"]) == (None, None)
    # the ledger read was unaffected
    assert (last["gate"], last["gate_label"], last["gate_checks"]) == (
        "verdict_only",
        "verdict only, nothing charged",
        ["verdict_fail"],
    )


@pytest.mark.asyncio
async def test_the_deciding_row_is_the_oldest_refusal_unless_one_settled(env) -> None:
    ac, db, h = env
    _firing(db, "wf-b", SLOT, "refused", detail="counts_match", run_id="r1")
    t0 = datetime(2026, 10, 5, 8, 1, tzinfo=UTC)
    # Inserted newest first, with distinct times: an asc→desc flip in the
    # ledger read would pick the newer refusal.
    _ledger(db, "r1", "refused", ["signature"], created_at=t0 + timedelta(minutes=5))
    _ledger(db, "r1", "refused", ["verdict_fail"], created_at=t0)
    last = await _last(ac, h["bob"])
    assert last["gate_checks"] == ["verdict_fail"]

    # any settled row decides the run, whatever its age
    for row in db.routinefiring.rows:
        row.update(state="settled", detail=None)
    _ledger(
        db,
        "r1",
        "settled",
        [],
        call_id="c1",
        created_at=t0 + timedelta(minutes=10),
    )
    last = await _last(ac, h["bob"])
    assert (last["gate"], last["gate_label"], last["gate_checks"]) == (
        "charged",
        None,
        [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_id", "gate", "gate_label"),
    [(None, "verdict_only", "verdict only, nothing charged"), ("c1", "charged", None)],
)
async def test_a_settled_firing_says_whether_its_decision_moved_money(
    env, call_id, gate, gate_label
) -> None:
    ac, db, h = env
    _firing(db, "wf-b", SLOT, "settled", run_id="r1", session_id="s1")
    _ledger(db, "r1", "settled", [], call_id=call_id)
    last = await _last(ac, h["bob"])
    assert last["label"] == "settled"
    assert (last["gate"], last["gate_label"]) == (gate, gate_label)


@pytest.mark.asyncio
async def test_an_unsettled_firing_notes_only_a_decision_that_is_absent(env) -> None:
    ac, db, h = env
    _firing(db, "wf-b", SLOT, "attested_unsettled", run_id="r1", session_id="s1")
    last = await _last(ac, h["bob"])
    assert last["label"] == "attested but unsettled"
    assert last["note"] == "no settlement decision is recorded for this run"
    assert last["gate"] is None  # the row's state is the truth (AD-22)
    # a ledger row exists: the firing is not yet reconciled, so no note
    _ledger(db, "r1", "refused", ["verdict_fail"])
    last = await _last(ac, h["bob"])
    assert last["note"] is None
    assert last["gate"] is None


@pytest.mark.asyncio
async def test_no_pane_label_says_done_success_or_verified(env) -> None:
    ac, db, h = env
    cases = [
        ("scheduled", {}),
        ("running", {"session_id": "s-run"}),
        ("attested_unsettled", {"run_id": "r-au", "session_id": "s-au"}),
        ("settled", {"run_id": "r-se", "session_id": "s-se"}),
        ("refused", {"run_id": "r-re", "session_id": "s-re", "detail": "signature"}),
        ("refused", {"run_id": "r-rn", "session_id": "s-rn"}),
        ("paused", {"session_id": "s-pa", "detail": "awaiting approval: x"}),
        ("skipped", {"detail": "overlap"}),
        ("error", {"detail": "restart"}),
    ]
    vocabulary = {
        "scheduled",
        "running",
        "attested but unsettled",
        "settled",
        "refused",
        "paused",
        "skipped",
        "error",
    }
    criteria = [{}, {"counts_match": True}, {"citations_required": True}]
    for i, (state, extra) in enumerate(cases):
        rid = f"wf-{i}"
        _routine(db, rid, "bob", criteria=criteria[i % len(criteria)])
        _firing(db, rid, SLOT, state, **extra)
        if "run_id" in extra:
            _envelope(db, extra["run_id"], extra["session_id"], [COUNTS_FAIL])
            if state == "settled":
                _ledger(db, extra["run_id"], "settled", [], call_id="c1")
            elif state == "refused":
                _ledger(db, extra["run_id"], "refused", ["verdict_fail"])
    resp = await ac.get("/api/v1/workflows", headers=h["bob"])
    seen = 0
    for wf in resp.json():
        last = wf["last_firing"]
        if last is None:
            continue
        seen += 1
        assert last["label"].split(" — ")[0] in vocabulary
        for field in ("label", "note", "gate_label", "checks_label"):
            assert not FORBIDDEN.search(last[field] or ""), (field, last[field])
    assert seen == len(cases)


@pytest.mark.asyncio
async def test_no_check_is_read_from_an_envelope_the_gate_could_not_verify(env) -> None:
    """Refused for its signature: the stored verdicts are no evidence, so the
    pane says no "checked"; the receipt itself is still there to inspect."""
    ac, db, h = env
    _criteria(db, "wf-b", {"counts_match": True})
    _firing(
        db, "wf-b", SLOT, "refused", detail="signature", run_id="r1", session_id="s1"
    )
    _envelope(
        db,
        "r1",
        "s1",
        [{"check": "counts_match", "result": "pass", "detail": "left=3 right=3"}],
    )
    _ledger(db, "r1", "refused", ["signature"], session_id="s1")
    last = await _last(ac, h["bob"])
    assert last["label"] == "refused — signature"
    assert (last["checks"], last["checks_label"]) == (None, None)
    assert last["receipt_available"] is True
