"""settle_invocation gating flow (Story 2.1, AC1/AC3/AC4).

Ordering is the point of these tests: the defer map write must land before
any suspending call, and the gate must complete before the credit block.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from superagent.pricing import settlement
from superagent.pricing.settlement import settle_invocation

CHARTER = "b" * 64


class FakeRedis:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def delete(self, key: str) -> None:
        self._calls.append("redis_release")


class FakeRedisCM:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def __aenter__(self) -> FakeRedis:
        return FakeRedis(self._calls)

    async def __aexit__(self, *args: Any) -> None:
        return None


def _unwrap(value: Any) -> Any:
    """Prisma ``Json`` wrapper → its data; anything else unchanged."""
    return getattr(value, "data", value)


class FakeTable:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        self.rows.append(data)
        return SimpleNamespace(**data)

    async def find_first(self, where: dict[str, Any]) -> None:
        return None


class FakeTxClient:
    """The client ``FakePrisma.tx()`` yields: the same tables, a distinct object.

    Distinct on purpose — the gate must be handed the transaction client for
    the claim and the plain client for refusals, and a test can only assert
    that if the two are different objects.
    """

    def __init__(self, client: FakePrisma) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class FakeTx:
    def __init__(self, client: FakePrisma) -> None:
        self._client = client

    async def __aenter__(self) -> FakeTxClient:
        self._client.tx_log.append("begin")
        return FakeTxClient(self._client)

    async def __aexit__(self, exc_type: Any, *args: Any) -> None:
        self._client.tx_log.append("rollback" if exc_type else "commit")


class FakeUserTable:
    async def find_unique(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return SimpleNamespace(credits_usd=100.0, arrears_usd=0.0, arrears_flag=False)

    async def update(self, where: dict[str, Any], data: dict[str, Any]) -> None:
        return None


class FakeAgentTable:
    async def find_unique(self, where: dict[str, Any]) -> None:
        return None


class FakePrisma:
    """Stand-in for src.generated_client.Prisma (construction patched in tests)."""

    instances: list[FakePrisma] = []

    def __init__(self) -> None:
        self.transaction = FakeTable()
        self.agentinvocation = FakeTable()
        # The gate writes its audit row through this client when it is given
        # no db of its own, which is how settle_invocation calls it.
        self.attestedsettlement = FakeTable()
        self.user = FakeUserTable()
        self.agent = FakeAgentTable()
        self.raw_calls: list[str] = []
        self.tx_log: list[str] = []
        FakePrisma.instances.append(self)

    def tx(self, **_: Any) -> FakeTx:
        return FakeTx(self)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def execute_raw(self, query: str, *args: Any) -> None:
        self.raw_calls.append(query)


@pytest.fixture()
def flow(monkeypatch: pytest.MonkeyPatch):
    """Wire all externals; returns call-order log and state."""
    calls: list[str] = []

    FakePrisma.instances = []
    monkeypatch.setattr("src.generated_client.Prisma", FakePrisma, raising=True)
    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: FakeRedisCM(calls))
    return {"calls": calls}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base = {
        "user_id": "u1",
        "agent_id": "a1",
        "session_id": "sess-1",
        "call_id": "c1",
        "base_fee": Decimal("1.00"),
        "latency_ms": 10,
        "execution_success": True,
    }
    return {**base, **overrides}


@pytest.fixture(autouse=True)
def _clean_deferred():
    settlement._deferred_settles.clear()
    yield
    settlement._deferred_settles.clear()


async def test_flag_on_defers_mid_run_before_any_await(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings

    monkeypatch.setattr(settings, "settlement_require_attestation", True)

    await settle_invocation(**_kwargs())

    assert settlement._deferred_settles.get("sess-1") is not None
    # Reserve still released; defer map write precedes it (no await before it).
    assert flow["calls"] == ["redis_release"]
    # No credit: no Transaction, no raw deduction.
    prisma = FakePrisma.instances[0] if FakePrisma.instances else None
    if prisma is not None:
        assert prisma.transaction.rows == []
        assert prisma.raw_calls == []


async def test_flag_on_run_id_gate_settles_then_credit(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)

    order: list[str] = []

    async def _spy_gate(**kwargs: Any) -> dict[str, Any]:
        order.append("gate")
        return {"outcome": "settled", "failed_checks": [], "envelope_digest": "d" * 64}

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _spy_gate)

    class _SpyTable(FakeTable):
        async def create(self, data: dict[str, Any]) -> SimpleNamespace:
            order.append("credit_write")
            return await super().create(data)

    class _SpyPrisma(FakePrisma):
        def __init__(self) -> None:
            super().__init__()
            self.transaction = _SpyTable()

    monkeypatch.setattr("src.generated_client.Prisma", _SpyPrisma)

    await settle_invocation(run_id="run-a", **_kwargs())

    assert order == ["gate", "credit_write"]  # verify before clear
    assert FakePrisma.instances[-1].transaction.rows[0]["status"] == "PENDING"
    # Claim and credit in one transaction, committed once (Story 2.4).
    assert FakePrisma.instances[-1].tx_log == ["begin", "commit"]
    assert (
        "sess-1" not in settlement._deferred_settles
    )  # not deferred when run_id given


async def test_flag_on_gate_refused_skips_credit(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)

    async def _refuse_gate(**kwargs: Any) -> dict[str, Any]:
        return {
            "outcome": "refused",
            "failed_checks": ["missing_attestation"],
            "envelope_digest": "",
        }

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _refuse_gate)

    await settle_invocation(run_id="run-b", **_kwargs())

    # Refused: the credit block never runs — no Prisma client is even built,
    # or any built client wrote nothing.
    for prisma in FakePrisma.instances:
        assert prisma.transaction.rows == []
        assert prisma.raw_calls == []


async def test_flag_on_failed_call_unchanged_no_defer(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings

    monkeypatch.setattr(settings, "settlement_require_attestation", True)

    await settle_invocation(**_kwargs(execution_success=False))

    assert settlement._deferred_settles == {}
    prisma = FakePrisma.instances[0]
    assert prisma.transaction.rows == []  # ERROR path — no billing
    assert prisma.agentinvocation.rows[0]["status"] == "ERROR"


async def test_flag_on_a_real_gate_refusal_writes_no_credit(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 2.2 — "no settled credit exists for that run_id", against the real
    gate rather than a spy.

    The other refusal test here substitutes the gate for a stub that returns a
    refusal; this one lets the real gate reach its own verdict. The run has no
    persisted attestation, so ``gate_attested_settle`` refuses
    ``missing_attestation`` — and the credit block below it must not run.
    """
    import validator.run_envelope
    from superagent.config import settings

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)

    async def _no_attestation(run_id: str, db: Any = None) -> None:
        return None

    monkeypatch.setattr(
        validator.run_envelope, "get_run_attestation_by_run_id", _no_attestation
    )

    await settle_invocation(run_id="run-unattested", **_kwargs())

    audited = [
        row
        for prisma in FakePrisma.instances
        for row in prisma.attestedsettlement.rows
        if row["run_id"] == "run-unattested"
    ]
    assert audited, "a refusal is still audited (AD-6)"
    assert all(row["outcome"] == "refused" for row in audited)
    assert all(row["settled_run_id"] is None for row in audited)

    # And no credit exists for the run: no Transaction row, no raw deduction.
    for prisma in FakePrisma.instances:
        assert prisma.transaction.rows == []
        assert prisma.raw_calls == []


async def test_flag_off_stock_behaviour(flow, monkeypatch: pytest.MonkeyPatch) -> None:
    from superagent.config import settings

    monkeypatch.setattr(settings, "settlement_require_attestation", False)

    await settle_invocation(**_kwargs())

    assert settlement._deferred_settles == {}
    prisma = FakePrisma.instances[0]
    assert prisma.transaction.rows[0]["status"] == "PENDING"
    assert prisma.raw_calls  # credit deducted


# ── Story 2.4: the claim and the credit share one transaction ───────────────


async def test_gate_receives_the_transaction_client_and_a_separate_audit_client(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)

    seen: dict[str, Any] = {}

    async def _spy_gate(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"outcome": "settled", "failed_checks": [], "envelope_digest": "d" * 64}

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _spy_gate)

    await settle_invocation(run_id="run-tx", **_kwargs())

    prisma = FakePrisma.instances[-1]
    assert isinstance(seen["db"], FakeTxClient)  # the claim goes inside the tx
    assert seen["audit_db"] is prisma  # refusals go around it
    assert seen["call_id"] == "c1"
    assert prisma.tx_log == ["begin", "commit"]


async def test_gate_refusal_leaves_the_transaction_empty_and_rolled_back(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)

    async def _refuse_gate(**kwargs: Any) -> dict[str, Any]:
        return {
            "outcome": "refused",
            "failed_checks": ["missing_attestation"],
            "envelope_digest": "",
        }

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _refuse_gate)

    await settle_invocation(run_id="run-refused", **_kwargs())

    prisma = FakePrisma.instances[-1]
    assert prisma.tx_log == ["begin", "rollback"]
    assert prisma.raw_calls == []
    assert prisma.transaction.rows == []
    assert prisma.agentinvocation.rows == []


async def test_credit_write_failure_rolls_back_and_is_audited_outside_the_tx(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the claim, a failed credit write must not leave a settled run.

    The fake cannot undo writes, so the rollback itself is proved against a
    real Postgres in test_settle_atomicity_pg.py. What this proves is the
    control flow: the transaction is rolled back, no Transaction row and no
    metrics row exist, and the attempt is audited as ``credit_write_error``
    on the plain client, with the call it belonged to.
    """
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)

    async def _settled_gate(**kwargs: Any) -> dict[str, Any]:
        return {"outcome": "settled", "failed_checks": [], "envelope_digest": "d" * 64}

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _settled_gate)

    async def _boom(tx: Any, **kwargs: Any) -> None:
        raise RuntimeError("journal write failed")

    monkeypatch.setattr(settlement, "_record_transaction", _boom)

    await settle_invocation(run_id="run-cw", **_kwargs())  # never raises

    prisma = FakePrisma.instances[-1]
    assert prisma.tx_log == ["begin", "rollback"]
    assert prisma.transaction.rows == []
    assert prisma.agentinvocation.rows == []
    audited = [r for r in prisma.attestedsettlement.rows if r["run_id"] == "run-cw"]
    assert len(audited) == 1
    assert audited[0]["outcome"] == "refused"
    assert audited[0]["settled_run_id"] is None
    assert _unwrap(audited[0]["failed_checks"]) == ["credit_write_error"]
    assert audited[0]["call_id"] == "c1"
    assert audited[0]["charter_hash"] == CHARTER


async def test_revenue_split_error_refuses_before_the_gate_and_before_any_write(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A split that cannot be computed claims nothing and charges nothing."""
    from superagent.config import settings
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    monkeypatch.setattr(settings, "run_attestation_charter_hash", CHARTER)
    monkeypatch.setenv("COORDINATOR_SHARE_BPS", "not-a-number")

    gate_calls: list[dict[str, Any]] = []

    async def _spy_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls.append(kwargs)
        return {"outcome": "settled", "failed_checks": [], "envelope_digest": "d" * 64}

    monkeypatch.setattr(settle_gate, "gate_attested_settle", _spy_gate)

    await settle_invocation(run_id="run-cfg", **_kwargs())

    assert gate_calls == []  # nothing was claimed
    for prisma in FakePrisma.instances:
        assert prisma.tx_log == []
        assert prisma.raw_calls == []
        assert prisma.transaction.rows == []
        assert prisma.attestedsettlement.rows == []


async def test_flag_off_credit_is_still_one_committed_transaction(
    flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-9: the stock path writes the same rows, now atomically."""
    from superagent.config import settings

    monkeypatch.setattr(settings, "settlement_require_attestation", False)

    await settle_invocation(**_kwargs())

    prisma = FakePrisma.instances[0]
    assert prisma.tx_log == ["begin", "commit"]
    assert prisma.raw_calls  # credit deducted
    assert prisma.transaction.rows[0]["status"] == "PENDING"
    assert prisma.agentinvocation.rows[0]["status"] == "SUCCESS"
    assert prisma.attestedsettlement.rows == []  # no gate, no audit row
