"""Payment settlement — Step 6.5 of the ExecutionMiddleware pipeline.

Fires after checklist auto-update.  Runs as asyncio.create_task so it never
blocks the caller's response.

On success: deducts credits_usd, writes Transaction(PENDING) and AgentInvocation(SUCCESS).
On error/timeout: releases reserve, writes AgentInvocation(ERROR/TIMEOUT).

MCP agents are free — settlement is never called for protocol == "MCP".

Atomicity (Story 2.4, AR-11): the credit deduction and the ``Transaction``
row commit in one interactive transaction — and, on the attestation-gated
path, the settled audit row (the claim) is written inside that same
transaction by ``settle_gate.gate_attested_settle``. Either everything a
settle records is visible, or none of it is; a run whose credit write fails
is rolled back to unclaimed, audited as ``credit_write_error``, and stays
eligible to settle again. The revenue split is computed before the
transaction opens, so a configuration error refuses a settle rather than
rolling one back. Metrics (``AgentInvocation``, the agent's rolling success
rate) are written after the commit, outside the transaction: they are not
money, and the ``agents`` row is shared by every user of that agent.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# Deferred settle map (KYA slice, Story 2.1 AC4): when
# SETTLEMENT_REQUIRE_ATTESTATION is true, per-call step-6.5 settles defer
# (the run's envelope only seals at run completion). Keyed by session_id;
# popped by SettlementGateObserver post-seal. Per-process, bounded — oldest
# evicted with a warning (same idiom as the observer's envelope bound).
# Scope: one charged call per run; later calls overwrite (post-MVP: accumulate).
MAX_DEFERRED_SETTLES = 1000
_deferred_settles: dict[str, dict[str, Any]] = {}


def _store_deferred(session_id: str, kwargs: dict[str, Any]) -> None:
    if session_id in _deferred_settles:
        # Multi-charged-call signal: the slice scope keeps only the last
        # call's kwargs (per-run fee accumulation is post-MVP) — make the
        # overwrite visible instead of silent (2.1 review hygiene).
        logger.warning(
            "settle_invocation: session %s already has a deferred settle — "
            "overwriting (multiple charged calls in one run; only the last "
            "is billed in the slice)",
            session_id,
        )
    _deferred_settles[session_id] = kwargs
    while len(_deferred_settles) > MAX_DEFERRED_SETTLES:
        evicted = next(iter(_deferred_settles))
        del _deferred_settles[evicted]
        logger.warning(
            "settle_invocation: deferred-settle bound (%d) reached — evicted %s",
            MAX_DEFERRED_SETTLES,
            evicted,
        )


def pop_deferred_settle(session_id: str) -> dict[str, Any] | None:
    """Pop a session's deferred settle kwargs (None when nothing deferred)."""
    return _deferred_settles.pop(session_id, None)


def read_share_bps() -> tuple[int, int]:
    """Return ``(coordinator_bps, validator_bps)`` from the environment.

    Raises ``ValueError`` naming the variable when a value is not an integer,
    is negative, or the two shares together exceed the whole fee. Called on
    every settle by :func:`compute_revenue_split` and once at boot by
    ``settle_gate.validate_gate_config``, so a misconfiguration refuses to
    boot instead of failing every settle.
    """
    from .settle_gate import SHARE_BPS_TOTAL  # noqa: PLC0415

    shares: list[int] = []
    for name in ("COORDINATOR_SHARE_BPS", "VALIDATOR_SHARE_BPS"):
        raw = os.getenv(name, "0")
        try:
            value = int(raw.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}={raw!r} is not an integer") from exc
        if value < 0:
            raise ValueError(f"{name}={value} must not be negative")
        shares.append(value)
    coordinator_bps, validator_bps = shares
    if coordinator_bps + validator_bps > SHARE_BPS_TOTAL:
        raise ValueError(
            f"COORDINATOR_SHARE_BPS + VALIDATOR_SHARE_BPS = "
            f"{coordinator_bps + validator_bps} exceeds {SHARE_BPS_TOTAL}"
        )
    return coordinator_bps, validator_bps


def compute_revenue_split(base_fee: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Return (developer_payout, platform_cut, validator_cut) for settlement.

    Uses the three-way split when COORDINATOR_SHARE_BPS or VALIDATOR_SHARE_BPS
    are set; otherwise falls back to the legacy two-way platform split.
    """
    coordinator_bps, validator_bps = read_share_bps()

    if coordinator_bps > 0 or validator_bps > 0:
        from common_pricing.formulae import split_revenue_three_way

        developer_payout, validator_cut, coordinator_cut = split_revenue_three_way(
            base_fee,
            coordinator_share_bps=coordinator_bps,
            validator_share_bps=validator_bps,
        )
        return developer_payout, coordinator_cut, validator_cut

    from common_pricing.formulae import split_revenue

    developer_payout, platform_cut = split_revenue(base_fee)
    return developer_payout, platform_cut, Decimal("0")


async def settle_invocation(
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    call_id: str,
    base_fee: Decimal,
    latency_ms: int,
    execution_success: bool,
    platform_tokens: int = 0,
    run_id: str | None = None,
) -> None:
    """
    Step 6.5 — Release reserve, deduct credits, write Transaction(PENDING).

    Called via asyncio.create_task — exceptions are logged but never re-raised.

    When SETTLEMENT_REQUIRE_ATTESTATION is true (AD-1/AD-2): a call without a
    ``run_id`` (the mid-run step-6.5 call) defers — the map write happens
    BEFORE any suspending call so the post-seal gate observer can never pop
    an empty map; the reserve is still released; no credit is written. A
    call WITH a ``run_id`` (the post-seal replay) runs the gate first and
    writes credit only on a settled outcome. Failed executions take the
    stock ERROR path regardless of the flag.
    """
    from ..config import settings

    # (1) Sync flag-check + defer — FIRST, before any await (FIFO race guard).
    gate_required = settings.settlement_require_attestation and execution_success
    deferred = gate_required and run_id is None
    if deferred:
        _store_deferred(
            session_id,
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "call_id": call_id,
                "base_fee": base_fee,
                "latency_ms": latency_ms,
                "platform_tokens": platform_tokens,
                "execution_success": execution_success,
            },
        )

    # ── Release Redis reserve (always — success or failure) ───────────────────
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        async with redis_client as r:
            await r.delete(f"reserve:{session_id}:{call_id}")
    except Exception:
        logger.warning(
            "settle_invocation: Redis unavailable — reserve not released call_id=%s",
            call_id,
        )

    # (3) Deferred mid-run settle: envelope not yet sealed — nothing to do.
    if deferred:
        logger.info(
            "settle_invocation: settle deferred to post-seal gate session=%s call_id=%s",
            session_id,
            call_id,
        )
        return

    # ── Invocation record (ERROR path — no billing) ───────────────────────────
    if not execution_success:
        await _write_invocation_record(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            call_id=call_id,
            status="ERROR",
            latency_ms=latency_ms,
            base_fee=None,
            platform_tokens=platform_tokens,
        )
        return

    # ── Revenue split — before any write ─────────────────────────────────────
    # Computed ahead of the transaction so that a configuration error refuses
    # the settle with nothing claimed and nothing charged.
    try:
        developer_payout, platform_cut, validator_cut = compute_revenue_split(base_fee)
        validator_did = os.getenv("VALIDATOR_DID", "").strip()
        if validator_did and validator_cut > 0:
            logger.info(
                "settle_invocation: mock validator payout validator_did=%s "
                "amount=%s call_id=%s",
                validator_did,
                validator_cut,
                call_id,
            )
    except Exception:
        logger.exception(
            "settle_invocation: revenue split failed call_id=%s — nothing "
            "claimed, nothing charged",
            call_id,
        )
        return

    # ── Gate + credit in one transaction, then metrics ────────────────────────
    try:
        from src.generated_client import Prisma

        db = Prisma()
        await db.connect()
        try:
            settled = await _settle_in_one_transaction(
                db,
                gate_required=gate_required,
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                call_id=call_id,
                base_fee=base_fee,
                platform_cut=platform_cut,
                developer_payout=developer_payout,
                latency_ms=latency_ms,
                expected_charter_hash=settings.run_attestation_charter_hash,
            )
            if not settled:
                return

            # Metrics, after the commit and outside the transaction: not
            # money, and the agents row is shared by every user of the agent.
            await db.agentinvocation.create(
                data={
                    "session_id": session_id,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "call_id": call_id,
                    "status": "SUCCESS",
                    "latency_ms": latency_ms,
                    "base_fee": float(base_fee),
                    "platform_tokens": platform_tokens,
                }
            )
            await _update_agent_metrics(agent_id, success=True, db=db)

        finally:
            await db.disconnect()

    except Exception:
        logger.exception(
            "settle_invocation: DB write failed call_id=%s agent=%s user=%s",
            call_id,
            agent_id,
            user_id,
        )


class _GateRefused(Exception):
    """Raised inside the settle transaction to leave it with no credit written."""


async def _settle_in_one_transaction(
    db: Any,
    *,
    gate_required: bool,
    run_id: str | None,
    session_id: str,
    user_id: str,
    agent_id: str,
    call_id: str,
    base_fee: Decimal,
    platform_cut: Decimal,
    developer_payout: Decimal,
    latency_ms: int,
    expected_charter_hash: str | None,
) -> bool:
    """Claim and credit in one transaction; both commit or neither does.

    Returns True when the credit committed. A gate refusal returns False with
    nothing written on this transaction (the refusal itself is audited by the
    gate on ``db``, outside the transaction). Any other failure rolls the
    transaction back — the claim included — audits it as
    ``credit_write_error`` on ``db``, and re-raises for the caller's log.

    Isolation is READ COMMITTED, the default. The ``UPDATE users`` takes a
    row lock on the user's balance; a concurrent settle for the same user
    waits on that lock until this transaction ends and then re-evaluates
    against the committed balance. The lock serialises the balance, so no
    higher isolation level is needed.
    """
    gate_outcome: dict[str, Any] | None = None
    try:
        async with db.tx() as tx:
            if gate_required:
                from .settle_gate import gate_attested_settle  # noqa: PLC0415

                gate_outcome = await gate_attested_settle(
                    run_id=run_id,
                    session_id=session_id,
                    expected_charter_hash=expected_charter_hash,
                    db=tx,
                    audit_db=db,
                    call_id=call_id,
                )
                if gate_outcome["outcome"] != "settled":
                    raise _GateRefused()
            await _deduct_credits(tx, user_id=user_id, base_fee=base_fee)
            await _record_transaction(
                tx,
                session_id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                call_id=call_id,
                base_fee=base_fee,
                platform_cut=platform_cut,
                developer_payout=developer_payout,
                latency_ms=latency_ms,
            )
    except _GateRefused:
        assert gate_outcome is not None
        logger.warning(
            "settle_invocation: gate refused run=%s checks=%s — no credit",
            run_id,
            ",".join(gate_outcome["failed_checks"]),
        )
        return False
    except Exception:
        if gate_outcome is not None and gate_outcome["outcome"] == "settled":
            # The claim rolled back with the credit. Record the attempt on the
            # non-transactional client so the audit trail keeps it; the run
            # is unclaimed and may be settled again.
            await _audit_credit_write_failure(
                db,
                run_id=run_id,
                session_id=session_id,
                call_id=call_id,
                envelope_digest=gate_outcome["envelope_digest"],
                charter_hash=expected_charter_hash,
            )
        raise
    return True


async def _deduct_credits(tx: Any, *, user_id: str, base_fee: Decimal) -> None:
    """Debit the user's balance (floored at 0) and flag any shortfall."""
    await tx.execute_raw(
        "UPDATE users SET credits_usd = GREATEST(credits_usd - $1::numeric, 0) WHERE id = $2",
        float(base_fee),
        user_id,
    )
    user = await tx.user.find_unique(where={"id": user_id})
    if user is not None and float(user.credits_usd) == 0 and base_fee > 0:
        shortfall = base_fee - Decimal(str(user.credits_usd))
        if shortfall > 0:
            await tx.user.update(
                where={"id": user_id},
                data={
                    "arrears_usd": float(shortfall),
                    "arrears_flag": True,
                    "credits_usd": 0.0,
                },
            )


async def _record_transaction(
    tx: Any,
    *,
    session_id: str,
    user_id: str,
    agent_id: str,
    call_id: str,
    base_fee: Decimal,
    platform_cut: Decimal,
    developer_payout: Decimal,
    latency_ms: int,
) -> None:
    """Write Transaction(PENDING) — the Gateway settlement cron settles on-chain."""
    await tx.transaction.create(
        data={
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "call_id": call_id,
            "base_fee": float(base_fee),
            "platform_cut": float(platform_cut),
            "developer_payout": float(developer_payout),
            "latency_ms": latency_ms,
            "status": "PENDING",
        }
    )


async def _audit_credit_write_failure(
    db: Any,
    *,
    run_id: str | None,
    session_id: str,
    call_id: str,
    envelope_digest: str,
    charter_hash: str | None,
) -> None:
    from .settle_gate import CHECK_CREDIT_WRITE, _write_outcome  # noqa: PLC0415

    try:
        await _write_outcome(
            db,
            run_id=run_id or "",
            session_id=session_id,
            outcome="refused",
            envelope_digest=envelope_digest,
            failed_checks=[CHECK_CREDIT_WRITE],
            charter_hash=charter_hash,
            call_id=call_id,
        )
    except Exception:
        logger.exception(
            "settle_invocation: credit-write-failure audit could not be written "
            "for run %s — the settle was rolled back either way",
            run_id,
        )


async def _write_invocation_record(
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    call_id: str,
    status: str,
    latency_ms: int,
    base_fee: Decimal | None,
    platform_tokens: int,
) -> None:
    try:
        from src.generated_client import Prisma

        db = Prisma()
        await db.connect()
        try:
            await db.agentinvocation.create(
                data={
                    "session_id": session_id,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "call_id": call_id,
                    "status": status,
                    "latency_ms": latency_ms,
                    "base_fee": float(base_fee) if base_fee is not None else None,
                    "platform_tokens": platform_tokens,
                }
            )
            await _update_agent_metrics(agent_id, success=(status == "SUCCESS"), db=db)
        finally:
            await db.disconnect()
    except Exception:
        logger.exception(
            "settle_invocation: invocation record write failed call_id=%s", call_id
        )


async def _update_agent_metrics(agent_id: str, success: bool, db: object) -> None:
    """Increment execution_count and update rolling success_rate on Agent row."""
    try:
        agent = await db.agent.find_unique(where={"id": agent_id})  # type: ignore[attr-defined]
        if agent is None:
            return
        total = agent.execution_count + 1
        current_rate = getattr(agent, "success_rate", 0.70)
        # Exponential moving average: weight recent calls more
        new_rate = (current_rate * (total - 1) + (1.0 if success else 0.0)) / total
        await db.agent.update(  # type: ignore[attr-defined]
            where={"id": agent_id},
            data={"execution_count": total, "success_rate": new_rate},
        )
    except Exception:
        logger.debug("_update_agent_metrics: failed for agent=%s", agent_id)
