"""Story 2.4 — the claim and the credit commit together, against a REAL Postgres.

``test_settle_gate_flow.py`` proves the control flow on a fake that cannot
undo a write. Only a real database can prove that a transaction boundary
holds: that after a failure at any point between the settled claim and the
journal row, the database carries no claim, no debit and no journal row, and
that the run can then be settled. This module drives the real
``settle_invocation`` end to end — a persisted attestation, the real gate,
the real transaction — with three kinds of failure:

- a fault raised inside the transaction at each write (claim → debit →
  journal), then a retry that must settle;
- a genuine process death in the middle of the transaction (``os._exit`` in
  a subprocess, the connection dies with it), then a retry that must settle;
- two concurrent settles of one run where the first rolls back — the second
  must be admitted and settle.

Point ``SETTLE_ATOMICITY_PG_URL`` (or ``SETTLE_IDEMPOTENCY_PG_URL``) at a
throwaway database that has had ``prisma migrate deploy`` applied. **A skip
here is not a pass**: without this module the atomicity acceptance criteria
are uncovered, and a run must be reported that way.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

PG_URL = os.getenv("SETTLE_ATOMICITY_PG_URL") or os.getenv("SETTLE_IDEMPOTENCY_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "SETTLE_ATOMICITY_PG_URL unset — Story 2.4 (claim and credit in one "
        "transaction) is NOT covered by this run. It needs a real Postgres."
    ),
)

CHARTER = "b" * 64
AGENT_DID = "did:orcha:agent:kya-demo"
FEE = Decimal("1.00")
START_CREDITS = 10.0


# ── Wiring ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from validator import signer

    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    """Real gate, real transaction, scratch database; Redis faked away."""
    import redis.asyncio as aioredis
    import src.generated_client as generated
    from superagent.config import settings
    from superagent.pricing import settlement

    real_prisma = generated.Prisma
    monkeypatch.setattr(
        generated,
        "Prisma",
        lambda **kw: real_prisma(**{"datasource": {"url": PG_URL}, **kw}),
    )
    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)

    class _Redis:
        async def __aenter__(self) -> _Redis:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def delete(self, key: str) -> None:
            return None

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: _Redis())
    settlement._deferred_settles.clear()
    yield
    settlement._deferred_settles.clear()


async def _client():
    from src.generated_client import Prisma

    db = Prisma(datasource={"url": PG_URL})
    await db.connect()
    return db


async def _new_user(db: Any) -> str:
    user = await db.user.create(
        data={
            "email": f"settle-{uuid.uuid4().hex[:12]}@example.test",
            "credits_usd": START_CREDITS,
        }
    )
    return user.id


async def _persist_attestation(db: Any, run_id: str, session_id: str) -> None:
    from validator.run_envelope import (
        build_run_envelope,
        persist_run_attestation,
        sign_run_envelope,
    )

    envelope = sign_run_envelope(
        build_run_envelope(
            run_id=run_id,
            agent_dids=[AGENT_DID],
            charter_hash=CHARTER,
            policy_version="p/1",
            steps=[
                {
                    "call_id": "c1",
                    "tool": "search_docs",
                    "args": {"q": "x"},
                    "output": "y",
                    "success": True,
                    "latency_ms": 10,
                }
            ],
            verdicts=[],
            started_at="2026-08-06T01:00:00Z",
            finished_at="2026-08-06T01:00:01Z",
            signer_did="did:orcha:system:validator",
        )
    )
    row = await persist_run_attestation(session_id, envelope, db=db)
    assert row is not None, "attestation must be persisted for the real lookup"


async def _settle(*, run_id: str, user_id: str, call_id: str, session_id: str) -> None:
    from superagent.pricing.settlement import settle_invocation

    await settle_invocation(
        user_id=user_id,
        agent_id=AGENT_DID,
        session_id=session_id,
        call_id=call_id,
        base_fee=FEE,
        latency_ms=10,
        execution_success=True,
        run_id=run_id,
    )


def _checks(row: Any) -> list[str]:
    return getattr(row.failed_checks, "data", row.failed_checks)


async def _state(db: Any, *, run_id: str, user_id: str) -> dict[str, Any]:
    """Everything a settle records, read on a fresh connection."""
    rows = await db.attestedsettlement.find_many(where={"run_id": run_id})
    user = await db.user.find_unique(where={"id": user_id})
    transactions = await db.transaction.find_many(where={"user_id": user_id})
    invocations = await db.agentinvocation.find_many(where={"user_id": user_id})
    return {
        "settled": [r for r in rows if r.outcome == "settled"],
        "refused": [r for r in rows if r.outcome == "refused"],
        "credits": float(user.credits_usd),
        "transactions": transactions,
        "invocations": invocations,
    }


def _assert_untouched(state: dict[str, Any]) -> None:
    """No claim, no debit, no journal row, no metrics row."""
    assert state["settled"] == []
    assert state["credits"] == START_CREDITS
    assert state["transactions"] == []
    assert state["invocations"] == []


def _assert_settled_once(state: dict[str, Any], *, run_id: str, call_id: str) -> None:
    (claim,) = state["settled"]
    assert claim.settled_run_id == run_id
    assert claim.call_id == call_id
    assert _checks(claim) == []
    assert state["credits"] == START_CREDITS - float(FEE)
    (journal,) = state["transactions"]
    assert journal.call_id == call_id
    assert str(journal.status).endswith("PENDING")
    (metrics,) = state["invocations"]
    assert metrics.call_id == call_id


# ── Positive control ─────────────────────────────────────────────────────────


async def test_a_settle_records_claim_debit_journal_and_metrics_together() -> None:
    db = await _client()
    try:
        user_id = await _new_user(db)
        run_id = f"run-ok-{uuid.uuid4().hex[:12]}"
        session_id = f"sess-{run_id}"
        await _persist_attestation(db, run_id, session_id)

        await _settle(
            run_id=run_id,
            user_id=user_id,
            call_id=f"{run_id}-call",
            session_id=session_id,
        )

        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_settled_once(state, run_id=run_id, call_id=f"{run_id}-call")
        assert state["refused"] == []
    finally:
        await db.disconnect()


# ── Fault injection at each write inside the transaction ────────────────────


@pytest.mark.parametrize(
    "fault_at",
    ["after_claim_before_debit", "after_debit_before_journal", "after_journal"],
)
async def test_a_failure_anywhere_after_the_claim_leaves_the_run_settleable(
    monkeypatch: pytest.MonkeyPatch, fault_at: str
) -> None:
    """Inject a failure at each commit point; the database must be untouched
    and the run must settle on retry — not refused as already settled."""
    from superagent.pricing import settlement

    real_deduct = settlement._deduct_credits
    real_record = settlement._record_transaction
    armed = {"on": True}

    async def _deduct(tx: Any, **kwargs: Any) -> None:
        if armed["on"] and fault_at == "after_claim_before_debit":
            raise RuntimeError("injected before the debit")
        await real_deduct(tx, **kwargs)

    async def _record(tx: Any, **kwargs: Any) -> None:
        if armed["on"] and fault_at == "after_debit_before_journal":
            raise RuntimeError("injected before the journal row")
        await real_record(tx, **kwargs)
        if armed["on"] and fault_at == "after_journal":
            raise RuntimeError("injected after the journal row, before commit")

    monkeypatch.setattr(settlement, "_deduct_credits", _deduct)
    monkeypatch.setattr(settlement, "_record_transaction", _record)

    db = await _client()
    try:
        user_id = await _new_user(db)
        run_id = f"run-{fault_at[:8]}-{uuid.uuid4().hex[:8]}"
        session_id = f"sess-{run_id}"
        await _persist_attestation(db, run_id, session_id)

        await _settle(
            run_id=run_id,
            user_id=user_id,
            call_id=f"{run_id}-call",
            session_id=session_id,
        )

        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_untouched(state)
        # The attempt is on the record, on the connection that survived.
        (audit,) = state["refused"]
        assert _checks(audit) == ["credit_write_error"]
        assert audit.settled_run_id is None
        assert audit.call_id == f"{run_id}-call"
        assert audit.charter_hash == CHARTER

        # Retry with the same run and the same call: eligible, and settles.
        armed["on"] = False
        await _settle(
            run_id=run_id,
            user_id=user_id,
            call_id=f"{run_id}-call",
            session_id=session_id,
        )

        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_settled_once(state, run_id=run_id, call_id=f"{run_id}-call")
        assert [_checks(r) for r in state["refused"]] == [["credit_write_error"]]
    finally:
        await db.disconnect()


# ── A real process death inside the transaction ─────────────────────────────


_CHILD = textwrap.dedent(
    """
    import asyncio, os, sys
    from decimal import Decimal

    run_id, call_id, user_id, session_id, charter, pg_url, marker = sys.argv[1:8]

    from superagent.config import settings
    settings.settlement_require_attestation = True
    settings.run_attestation_charter_hash = charter

    import src.generated_client as generated
    real_prisma = generated.Prisma
    generated.Prisma = lambda: real_prisma(datasource={"url": pg_url})

    import redis.asyncio as aioredis

    class _Redis:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def delete(self, key):
            return None

    aioredis.from_url = lambda *a, **k: _Redis()

    from superagent.pricing import settlement

    async def _die(tx, **kwargs):
        # The claim is written on the open transaction; die before the debit.
        with open(marker, "w") as fh:
            fh.write("claimed\\n")
        os._exit(137)

    settlement._deduct_credits = _die

    asyncio.run(
        settlement.settle_invocation(
            user_id=user_id,
            agent_id="did:orcha:agent:kya-demo",
            session_id=session_id,
            call_id=call_id,
            base_fee=Decimal("1.00"),
            latency_ms=10,
            execution_success=True,
            run_id=run_id,
        )
    )
    """
)


async def test_a_process_death_after_the_claim_leaves_the_run_settleable(
    tmp_path: Path,
) -> None:
    """The literal crash: the process dies with the transaction open.

    The child settles for real up to the claim, then ``os._exit``s before the
    debit. Its database session — held by the query engine it spawned — is
    torn down with the process tree, and Postgres discards the uncommitted
    transaction, so the claim never becomes visible. The retry then settles.

    The engine is a separate process. It is checked first that, even while
    that engine still lingers, nothing of the attempt is visible; then the
    whole tree is killed, as a container death would, before the retry.
    """
    db = await _client()
    try:
        user_id = await _new_user(db)
        run_id = f"run-kill-{uuid.uuid4().hex[:12]}"
        session_id = f"sess-{run_id}"
        await _persist_attestation(db, run_id, session_id)

        marker = tmp_path / "claimed"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (env.get("PYTHONPATH", ""), *sys.path) if p
        )
        with (tmp_path / "child.stderr").open("w") as stderr:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD,
                    run_id,
                    f"{run_id}-call",
                    user_id,
                    session_id,
                    CHARTER,
                    PG_URL,
                    str(marker),
                ],
                env=env,
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,  # the engine it spawns joins this group
            )
            try:
                returncode = child.wait(timeout=90)
            except subprocess.TimeoutExpired:
                _kill_group(child.pid)
                raise
        assert returncode == 137, (tmp_path / "child.stderr").read_text()[-2000:]
        assert marker.exists(), "the child never reached the claim"

        # The process is dead, its engine may still be up: nothing is visible.
        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_untouched(state)
        assert state["refused"] == []  # nobody survived to audit it

        _kill_group(child.pid)  # the rest of the tree, as a container death

        started = time.monotonic()
        await asyncio.wait_for(
            _settle(
                run_id=run_id,
                user_id=user_id,
                call_id=f"{run_id}-call",
                session_id=session_id,
            ),
            timeout=60,
        )
        elapsed = time.monotonic() - started

        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_settled_once(state, run_id=run_id, call_id=f"{run_id}-call")
        assert state["refused"] == []
        assert elapsed < 30, f"retry took {elapsed:.1f}s"
    finally:
        await db.disconnect()


def _kill_group(pgid: int) -> None:
    """SIGKILL every process left in the child's session; none is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


# ── Two settles of one run; the winner rolls back ───────────────────────────


async def test_when_the_first_settle_rolls_back_the_second_is_admitted_and_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race under the transaction: the second claim waits on the index.

    The first settle holds its claim for a while, then fails before the
    journal row and rolls back. The second settle, started meanwhile, blocks
    on the unique index until that rollback, is admitted, and settles. Exactly
    one claim, one debit, one journal row; the failed attempt is audited.
    """
    from superagent.pricing import settlement

    real_record = settlement._record_transaction
    hold_s = 1.5

    async def _record(tx: Any, **kwargs: Any) -> None:
        if kwargs["call_id"].endswith("-first"):
            await asyncio.sleep(hold_s)
            raise RuntimeError(
                "injected: the first settle fails after holding its claim"
            )
        await real_record(tx, **kwargs)

    monkeypatch.setattr(settlement, "_record_transaction", _record)

    db = await _client()
    try:
        user_id = await _new_user(db)
        run_id = f"run-race-{uuid.uuid4().hex[:12]}"
        session_id = f"sess-{run_id}"
        await _persist_attestation(db, run_id, session_id)

        first = asyncio.create_task(
            _settle(
                run_id=run_id,
                user_id=user_id,
                call_id=f"{run_id}-first",
                session_id=session_id,
            )
        )
        await asyncio.sleep(0.3)
        second_started = time.monotonic()
        second = asyncio.create_task(
            _settle(
                run_id=run_id,
                user_id=user_id,
                call_id=f"{run_id}-second",
                session_id=session_id,
            )
        )
        await asyncio.wait_for(asyncio.gather(first, second), timeout=60)
        second_elapsed = time.monotonic() - second_started

        # The second settle could not finish before the first rolled back.
        assert second_elapsed >= hold_s - 0.3 - 0.2, second_elapsed

        state = await _state(db, run_id=run_id, user_id=user_id)
        _assert_settled_once(state, run_id=run_id, call_id=f"{run_id}-second")
        (audit,) = state["refused"]
        assert _checks(audit) == ["credit_write_error"]
        assert audit.call_id == f"{run_id}-first"
    finally:
        await db.disconnect()
