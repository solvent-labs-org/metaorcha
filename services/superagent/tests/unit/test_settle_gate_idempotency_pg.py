"""Story 2.3 AC2 — the concurrency proof, against a REAL Postgres.

``test_settle_gate.py`` runs against an in-memory fake. A fake cannot prove
that a database constraint arbitrates a real race; it can only prove the gate
behaves correctly on each side of one. This module runs two genuinely
concurrent settle attempts, on two separate connections, against a real
Postgres carrying the unique index built by the Story 2.3 migration.

Point ``SETTLE_IDEMPOTENCY_PG_URL`` at a throwaway database that has had
``prisma migrate deploy`` applied.

**A skip here is not a pass.** AC2 is uncovered unless this module actually
ran, and it must be reported that way — a suite that goes green while its
only real-database test silently skipped is precisely the failure this story
exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

PG_URL = os.getenv("SETTLE_IDEMPOTENCY_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "SETTLE_IDEMPOTENCY_PG_URL unset — Story 2.3 AC2 (two concurrent "
        "settles) is NOT covered by this run. It needs a real Postgres."
    ),
)

CHARTER = "b" * 64


@pytest.fixture(autouse=True)
def _reset_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from validator import signer

    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()


def _build_envelope(run_id: str) -> dict[str, Any]:
    from validator.run_envelope import build_run_envelope, sign_run_envelope

    return sign_run_envelope(
        build_run_envelope(
            run_id=run_id,
            agent_dids=["did:orcha:agent:kya-demo"],
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


async def test_two_concurrent_settles_yield_exactly_one_settled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 — and the index, not the pre-check, must be what rejects the loser.

    The interleave is forced. A plain ``asyncio.gather`` is free to run the
    first call to completion before the second starts; the pre-check would
    then catch the duplicate, the unique index would never be exercised, and
    the test would pass having proved nothing. The barrier holds both callers
    until each has read "not claimed", which is exactly the state two racing
    settles are in, and only then lets both write.

    Both callers here hold plain clients, so the loser is rejected the moment
    it inserts. On the production path the claim is written inside the settle
    transaction (Story 2.4): there the loser's insert waits on the index until
    the winner's transaction ends, and is rejected on commit or admitted on
    rollback. That shape is proved in test_settle_atomicity_pg.py.
    """
    import validator.run_envelope
    from src.generated_client import Prisma
    from superagent.pricing import settle_gate

    run_id = f"run-race-{uuid.uuid4().hex[:12]}"
    envelope = _build_envelope(run_id)

    async def _fake_lookup(rid: str, db: Any = None) -> Any:
        return envelope

    monkeypatch.setattr(
        validator.run_envelope, "get_run_attestation_by_run_id", _fake_lookup
    )

    # Two independent clients — two connections, the production shape, since
    # _write_outcome opens its own client when none is passed.
    left, right = Prisma(datasource={"url": PG_URL}), Prisma(datasource={"url": PG_URL})
    await left.connect()
    await right.connect()

    barrier = asyncio.Barrier(2)
    real_precheck = settle_gate._settled_claim_exists

    async def _barriered_precheck(db: Any, rid: str) -> bool:
        claimed = await real_precheck(db, rid)
        # Both callers are now past the read and neither has written.
        await asyncio.wait_for(barrier.wait(), timeout=20)
        return claimed

    monkeypatch.setattr(settle_gate, "_settled_claim_exists", _barriered_precheck)

    # Spy on the detector so we can assert the DATABASE raised a unique
    # violation, rather than inferring it from a row count.
    real_detector = settle_gate._is_unique_violation
    rejected_by_index: list[BaseException] = []

    def _spy_detector(exc: BaseException) -> bool:
        verdict = real_detector(exc)
        if verdict:
            rejected_by_index.append(exc)
        return verdict

    monkeypatch.setattr(settle_gate, "_is_unique_violation", _spy_detector)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                settle_gate.gate_attested_settle(
                    run_id=run_id, expected_charter_hash=CHARTER, db=left
                ),
                settle_gate.gate_attested_settle(
                    run_id=run_id, expected_charter_hash=CHARTER, db=right
                ),
            ),
            timeout=60,
        )

        outcomes = sorted(r["outcome"] for r in results)
        assert outcomes == ["refused", "settled"], (
            f"expected exactly one settle and one refusal, got {outcomes}"
        )

        loser = next(r for r in results if r["outcome"] == "refused")
        assert loser["failed_checks"] == ["already_settled"]

        # THE assertion of this module: Postgres rejected the second insert.
        # Without this, one settled row could equally mean the calls
        # serialized and the constraint was never tested.
        assert len(rejected_by_index) == 1, (
            "the unique index did not reject a concurrent insert — the two "
            "attempts did not actually race"
        )

        settled = await left.attestedsettlement.find_many(
            where={"run_id": run_id, "outcome": "settled"}
        )
        assert len(settled) == 1
        assert settled[0].settled_run_id == run_id

        refused = await left.attestedsettlement.find_many(
            where={"run_id": run_id, "outcome": "refused"}
        )
        assert len(refused) == 1
        assert refused[0].settled_run_id is None
    finally:
        await left.disconnect()
        await right.disconnect()


async def test_the_unique_index_actually_exists_on_this_database() -> None:
    """Guard: the test above is meaningless against a database without it."""
    from src.generated_client import Prisma

    db = Prisma(datasource={"url": PG_URL})
    await db.connect()
    try:
        rows = await db.query_raw(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'attested_settlements' "
            "AND indexname = 'attested_settlements_settled_run_id_key'"
        )
        assert rows, "attested_settlements_settled_run_id_key is missing"
        assert "UNIQUE" in rows[0]["indexdef"].upper()
    finally:
        await db.disconnect()
