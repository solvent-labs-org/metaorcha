"""Attestation-gated settlement (KYA slice — AD-1, AD-5, AD-6, AD-9).

The gate is the mandatory precondition on mock credit writes when
``SETTLEMENT_REQUIRE_ATTESTATION`` is true: resolve the run's persisted RFC
0003 envelope, verify it with the single vendored verifier
(``emerge.run_attestation.verify_run_attestation`` — checks are NEVER
reimplemented here), enforce the MVP charter policy, and record every
outcome in ``attested_settlements``.

Fail-closed throughout (AD-6): missing attestation, failed checks, run_id
mismatch, charter null/wrong, a verify exception, or a signed
``verdicts[]`` ``fail`` all refuse — and every refuse is still audited.
A ``fail`` verdict is gate policy (``CHECK_VERDICT_FAIL``), not an SDK
check: ``orcha-sdk verify`` still returns valid on that envelope.

Idempotent per run (Story 2.3, AR-11): at most one ``outcome=settled`` row
can exist for a ``run_id``, enforced by a unique index on the
``settled_run_id`` claim column. A replay or a lost concurrent race refuses
with ``already_settled`` and writes no credit.

Scope (recorded at story time): one charged call per run for the slice. A
multi-step run with several charged calls keeps only the last call's
deferred settle kwargs; per-run fee accumulation is post-MVP.

Atomic with the credit it authorizes (Story 2.4, AR-11): the settled audit
row is the claim, and the claim and the credit write commit together or not
at all. ``settle_invocation`` opens one interactive transaction, passes its
transaction client here as ``db`` so the claim is written inside it, and
writes the credit on the same client before committing. Refusals are audited
through ``audit_db`` — a separate, non-transactional client — so a refused
row commits independently of the transaction and survives its rollback. A
settle whose credit write fails after the claim is rolled back and audited as
``credit_write_error``; the run stays eligible to settle again.

Known gap, deferred: the deferred settle itself lives in process memory
between the charged call and the run's seal (``settlement._deferred_settles``),
so it is not durable across a restart. Reserve pairing is likewise still open.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# failed_checks vocabulary (shared with Story 2.2's enumeration).
CHECK_MISSING = "missing_attestation"
CHECK_RUN_ID_MISMATCH = "run_id_mismatch"
CHECK_CHARTER = "charter_hash"
CHECK_VERIFY_ERROR = "verify_error"
CHECK_SIGNER_DID = "signer_did"
CHECK_AGENT_DID = "agent_did"
CHECK_AUDIT_WRITE = "audit_write_error"
CHECK_ALREADY_SETTLED = "already_settled"
# A verified, claimed settle whose credit write did not commit (Story 2.4).
# Not a verifier check: it never appears in ``verdict.checks`` and sits
# outside ``_VERIFIER_CHECK_ORDER``. The claim was rolled back with the
# credit, so the run is not settled and may be settled again.
CHECK_CREDIT_WRITE = "credit_write_error"
# A signed ``verdicts[]`` entry with ``result: fail``. Gate policy, not a
# verifier check: ``orcha-sdk verify`` still returns valid=True (the
# envelope is honest about the failure). The gate is what refuses to pay.
# Stays outside ``_VERIFIER_CHECK_ORDER`` so ``_failed_checks_in_order``
# never pretends the SDK ran this check. ``warn`` does not refuse.
CHECK_VERDICT_FAIL = "verdict_fail"

# Revenue-split shares, in basis points of the base fee. Read by
# ``settlement.read_share_bps`` on every settle and validated once at boot.
SHARE_BPS_TOTAL = 10_000

# Gate policy (review finding, 2.1): envelopes verify against their embedded
# key by RFC 0003 design, so a self-signed envelope from any key passes the
# SDK check. The MVP trust anchor is gate policy, like the charter bind:
# refuse envelopes whose signer DID is not the platform validator's. Full
# key pinning / CA is post-MVP (AD-9 deferred).
PLATFORM_SIGNER_DID = "did:orcha:system:validator"
# Platform profile for agent DIDs (RFC 0003, "Platform profile"): the envelope
# schema binds DID syntax only; the did:orcha method is enforced here.
PLATFORM_AGENT_DID_RE = re.compile(r"^did:orcha:(agent|system):[\x20-\x7e]+$")

# Verifier check order — the sequence the RFC 0003 algorithm evaluates in.
# ``steps_merkle_root`` names the check RFC 0003's commitment work adds; it is
# simply absent from the result dict until then, and absent keys are skipped.
_VERIFIER_CHECK_ORDER = ("schema", "steps_root", "steps_merkle_root", "signature")

# Bounded re-check for the deferred-settle entry (review finding, 2.1): the
# mid-run defer runs inside an asyncio.create_task, so on a fast run
# emit_run_complete can dispatch before that task has executed its
# synchronous defer write. The observer re-checks briefly before giving up.
# Cost: runs with no charged call wait the full budget at completion.
DEFER_RECHECKS = 10
DEFER_RECHECK_DELAY_S = 0.1


def _failed_checks_in_order(checks: dict[str, Any]) -> list[str]:
    """Name the check that failed — never one that did not run.

    Invariant: ``failed_checks`` contains only checks that ran and failed. The
    verifier evaluates in ``_VERIFIER_CHECK_ORDER`` and returns at the first
    failure, leaving every later check at the ``False`` it was initialised
    with. Reading that dict as a set of failures therefore reports a schema
    failure as schema *and* steps_root *and* signature — and an audit row that
    names checks which never executed is not naming the failed check (FR-9,
    AR-13). A third party reading it would conclude the signature was bad when
    nothing ever checked the signature.

    So: walk the order and stop at the first ``False``. Keys the result does
    not carry are skipped. A failing key the order does not name is reported
    as-is, because nothing here knows where it sits in the sequence — better an
    over-report than a check dropped in silence.
    """
    for name in _VERIFIER_CHECK_ORDER:
        if name in checks and not checks[name]:
            return [name]
    return [name for name, ok in checks.items() if not ok]


_VERDICT_RESULTS = frozenset({"pass", "fail", "warn"})


def _verdicts_policy_refuse(envelope: dict[str, Any]) -> list[str] | None:
    """Gate-policy read of signed ``verdicts[]``.

    Returns ``[CHECK_VERDICT_FAIL]`` if any entry is ``fail``, ``[]`` if
    every entry is ``pass``/``warn`` or the list is empty, and ``None``
    when the field is unreadable (caller maps that to ``CHECK_VERIFY_ERROR``).
    Observers compute these entries; they must never refuse. This is the
    generalisation of ``sign_case_attestation``'s fail-closed guard.
    """
    try:
        verdicts = envelope.get("verdicts", [])
        if verdicts is None:
            verdicts = []
        if not isinstance(verdicts, list):
            return None
        saw_fail = False
        for entry in verdicts:
            if not isinstance(entry, dict):
                return None
            result = entry.get("result")
            if result not in _VERDICT_RESULTS:
                return None
            if result == "fail":
                saw_fail = True
        return [CHECK_VERDICT_FAIL] if saw_fail else []
    except Exception:
        logger.exception("gate_attested_settle: verdicts policy read failed")
        return None


def validate_gate_config(settings: Any) -> None:
    """Boot-time guard against silent-free-tier flag combinations.

    ``SETTLEMENT_REQUIRE_ATTESTATION=true`` without
    ``RUN_ATTESTATION_ENABLED=true`` means every charged call defers and no
    observer ever replays it — nothing is ever billed. That is a
    misconfiguration, not a degraded mode: refuse to boot (review finding,
    2.1). A gate flag with no charter hash configured is survivable but
    settles nothing on the settling path — warn.
    """
    if settings.settlement_require_attestation and not settings.run_attestation_enabled:
        raise RuntimeError(
            "SETTLEMENT_REQUIRE_ATTESTATION=true requires "
            "RUN_ATTESTATION_ENABLED=true — without envelopes every charged "
            "call defers forever and is never billed (silent free tier). "
            "Enable both, or disable the gate."
        )
    if (
        settings.settlement_require_attestation
        and not settings.run_attestation_charter_hash
    ):
        logger.warning(
            "SETTLEMENT_REQUIRE_ATTESTATION=true with no "
            "RUN_ATTESTATION_CHARTER_HASH — the settling path requires a "
            "charter bind, so every gated settle will refuse (charter_hash). "
            "This deployment settles nothing."
        )
    # The revenue split is computed on every settle, gated or not, from two
    # environment variables. A value that cannot be parsed, or shares that
    # exceed the fee, would make every settle fail before any write; that is
    # a misconfiguration to refuse at boot, not to discover once per run.
    from .settlement import read_share_bps  # noqa: PLC0415

    try:
        read_share_bps()
    except ValueError as exc:
        raise RuntimeError(
            f"revenue split misconfigured: {exc} — no settle could complete; "
            "fix COORDINATOR_SHARE_BPS / VALIDATOR_SHARE_BPS or unset them"
        ) from exc


def _prisma_json(value: Any) -> Any:
    """Wrap a value for a Prisma Json field; fall back to raw (fake DBs).

    Local mirror of validator.signer._prisma_json — that helper is private
    to the optional validator package and must not be imported here.
    """
    try:
        from src.generated_client.fields import Json as PrismaJson

        return PrismaJson(value)
    except ImportError:
        return value


def _is_unique_violation(exc: BaseException) -> bool:
    """True when ``exc`` is a unique-constraint violation.

    Detection is layered on purpose. The precise check is Prisma's
    ``UniqueViolationError`` — ``prisma/engine/utils.py`` maps Postgres error
    code ``P2002`` onto it. The ``code`` attribute and the message sniff keep
    this working for the test fakes and for any client that surfaces the same
    condition differently.

    A miss is safe by construction: an unrecognised write failure falls
    through to ``CHECK_AUDIT_WRITE``, which also refuses and also writes no
    credit. A miss costs audit precision, never money.
    """
    try:
        from prisma.errors import UniqueViolationError  # noqa: PLC0415

        if isinstance(exc, UniqueViolationError):
            return True
    except ImportError:
        pass
    if getattr(exc, "code", None) == "P2002":
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return (
        "uniqueviolation" in text
        or "unique constraint" in text
        or "duplicate key" in text
    )


async def _settled_claim_exists(db: Any, run_id: str) -> bool:
    """True when a settled row already claims ``run_id`` (Story 2.3, AR-11)."""
    owns_db = False
    client = db
    if client is None:
        from src.generated_client import Prisma

        client = Prisma()
        await client.connect()
        owns_db = True
    try:
        row = await client.attestedsettlement.find_first(
            where={"settled_run_id": run_id}
        )
        return row is not None
    finally:
        if owns_db:
            await client.disconnect()


async def _write_outcome(
    db: Any,
    *,
    run_id: str,
    session_id: str | None,
    outcome: str,
    envelope_digest: str,
    failed_checks: list[str],
    charter_hash: str | None,
    call_id: str | None = None,
) -> None:
    owns_db = False
    client = db
    if client is None:
        from src.generated_client import Prisma

        client = Prisma()
        await client.connect()
        owns_db = True
    try:
        await client.attestedsettlement.create(
            data={
                "run_id": run_id,
                # The idempotency claim (Story 2.3): mirrors run_id on a
                # settled row, NULL on a refused one. NULLs are distinct in a
                # Postgres unique index, so refusals stay unconstrained.
                "settled_run_id": run_id if outcome == "settled" else None,
                "session_id": session_id,
                # The charged call this outcome settles or refuses (Story
                # 2.4): ties the audit row to its ``transactions`` row
                # exactly, instead of by session.
                "call_id": call_id,
                "outcome": outcome,
                "envelope_digest": envelope_digest,
                "failed_checks": _prisma_json(failed_checks),
                "charter_hash": charter_hash,
            }
        )
    finally:
        if owns_db:
            await client.disconnect()


async def gate_attested_settle(
    *,
    run_id: str,
    session_id: str | None = None,
    expected_charter_hash: str | None = None,
    expected_signer_did: str = PLATFORM_SIGNER_DID,
    db: Any = None,
    audit_db: Any = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Verify a run's attestation and record the settle/refuse outcome.

    Returns ``{"outcome": "settled"|"refused", "failed_checks": [...],
    "envelope_digest": str}``. Never raises — every failure mode is a
    refused outcome with the failed check(s) named (AD-6).

    ``db`` carries the lookup, the idempotency pre-check and the settled
    claim. It may be a transaction client: ``settle_invocation`` passes the
    client of the transaction that also writes the credit, so the claim and
    the credit commit together (Story 2.4). ``audit_db`` carries refused
    rows; it defaults to ``db`` and MUST be a separate, non-transactional
    client whenever ``db`` is transactional, so that a refusal commits
    independently of the transaction and survives its rollback.
    """
    if audit_db is None:
        audit_db = db
    # Lazy imports — validator/sdk are optional workspace packages (graceful
    # degrade idiom, same as main.py's observer install).
    from emerge.run_attestation import (  # noqa: PLC0415
        compute_envelope_digest,
        verify_run_attestation,
    )
    from validator.run_envelope import (  # noqa: PLC0415
        get_run_attestation_by_run_id,
    )

    async def _refuse(failed: list[str], digest: str, charter: str | None) -> dict:
        # A refused outcome must survive its own audit write failing — the
        # refusal stands either way; the missing row is logged loudly
        # (review finding, 2.1: "never raises" must actually hold).
        try:
            await _write_outcome(
                audit_db,
                run_id=run_id,
                session_id=session_id,
                outcome="refused",
                envelope_digest=digest,
                failed_checks=failed,
                charter_hash=charter,
                call_id=call_id,
            )
        except Exception:
            logger.exception(
                "gate_attested_settle: refuse-audit write FAILED for run %s — "
                "refusal stands, audit row missing",
                run_id,
            )
        logger.warning(
            "gate_attested_settle: REFUSED run=%s checks=%s",
            run_id,
            ",".join(failed),
        )
        return {
            "outcome": "refused",
            "failed_checks": failed,
            "envelope_digest": digest,
        }

    # Defensive: the lookup helper documents never-raise, but the gate's own
    # contract must not depend on another package honoring its docstring.
    try:
        envelope = await get_run_attestation_by_run_id(run_id, db=db)
    except Exception:
        logger.exception("gate_attested_settle: lookup raised for run %s", run_id)
        return await _refuse([CHECK_VERIFY_ERROR], digest="", charter=None)
    if envelope is None:
        return await _refuse([CHECK_MISSING], digest="", charter=None)

    try:
        digest = compute_envelope_digest(envelope)
    except Exception:
        logger.exception("gate_attested_settle: digest failed for run %s", run_id)
        return await _refuse([CHECK_VERIFY_ERROR], digest="", charter=None)
    charter = envelope.get("charter_hash")

    # Corruption guard: get_run_attestation_by_run_id resolves via find_unique
    # on run_id, so a mismatch is unreachable through the normal path — it
    # catches a corrupted row or a broken fake, and stays fail-closed.
    if envelope.get("run_id") != run_id:
        return await _refuse([CHECK_RUN_ID_MISMATCH], digest=digest, charter=charter)

    try:
        verdict = verify_run_attestation(envelope)
    except Exception:  # fail-closed on verifier errors (AD-6)
        logger.exception("gate_attested_settle: verifier raised for run %s", run_id)
        return await _refuse([CHECK_VERIFY_ERROR], digest=digest, charter=charter)
    if not verdict.valid:
        failed = _failed_checks_in_order(verdict.checks)
        return await _refuse(failed or [CHECK_VERIFY_ERROR], digest, charter)

    # Acceptance (gate policy): a signed fail verdict refuses settlement.
    # Integrity already passed — ``orcha-sdk verify`` is valid=True on this
    # envelope. The refusal is this check plus the audit row, never the
    # verifier exit code.
    verdict_refuse = _verdicts_policy_refuse(envelope)
    if verdict_refuse is None:
        return await _refuse([CHECK_VERIFY_ERROR], digest=digest, charter=charter)
    if verdict_refuse:
        return await _refuse(verdict_refuse, digest=digest, charter=charter)

    # Signer trust anchor is gate policy (review finding, 2.1): the SDK check
    # verifies the signature against the envelope's own embedded key, so any
    # self-signed envelope passes it. The gate additionally requires the
    # platform validator's DID.
    signer_did = (envelope.get("signer") or {}).get("did")
    if signer_did != expected_signer_did:
        return await _refuse([CHECK_SIGNER_DID], digest=digest, charter=charter)

    # Agent DIDs are gate policy too (RFC 0003 platform profile): the schema
    # accepts any DID method, the platform settles only its own namespace.
    agent_dids = envelope.get("agent_dids") or []
    if not all(
        isinstance(d, str) and PLATFORM_AGENT_DID_RE.match(d) for d in agent_dids
    ):
        return await _refuse([CHECK_AGENT_DID], digest=digest, charter=charter)

    # Charter bind is gate policy (AD-9), not an SDK check: the settling path
    # requires a configured expected hash matching the envelope's.
    if expected_charter_hash is None or charter != expected_charter_hash:
        return await _refuse([CHECK_CHARTER], digest=digest, charter=charter)

    # Idempotency, layer 1 of 2 (Story 2.3, AR-11) — the courtesy fast path.
    #
    # This turns the ordinary sequential replay into an explicit refuse that
    # names `already_settled` and carries the verified digest and charter into
    # the audit row. It runs AFTER verification on purpose: a tampered replay
    # of an already-settled run should be reported as tampered, not as a
    # duplicate, so the audit names the first thing actually wrong.
    #
    # It is TOCTOU-racy by construction — two concurrent attempts can both
    # pass it — and it is NOT the authority. The unique index on
    # `settled_run_id` is (layer 2, below). Do not delete that index believing
    # this check covers it.
    try:
        claimed = await _settled_claim_exists(db, run_id)
    except Exception:
        # A failed pre-check read is not fail-open: the write below still has
        # to get past the unique index, which is the actual arbiter.
        logger.exception(
            "gate_attested_settle: idempotency pre-check raised for run %s — "
            "deferring to the unique index",
            run_id,
        )
        claimed = False
    if claimed:
        return await _refuse([CHECK_ALREADY_SETTLED], digest=digest, charter=charter)

    # A settled outcome with no audit row must not exist: if the settled-audit
    # write fails, the settle itself is refused so no credit is written
    # (fail-closed — review finding, 2.1). When ``db`` is a transaction client
    # this write is the claim inside the settle transaction; it becomes
    # visible to other connections only when the credit commits with it.
    try:
        await _write_outcome(
            db,
            run_id=run_id,
            session_id=session_id,
            outcome="settled",
            envelope_digest=digest,
            failed_checks=[],
            charter_hash=charter,
            call_id=call_id,
        )
    except Exception as exc:
        # Idempotency, layer 2 of 2 — the authority. A unique violation here
        # is not a broken database: a concurrent attempt won the race and
        # claimed this run. Both attempts verified, both passed the pre-check,
        # and Postgres picked exactly one. Name it precisely so the audit can
        # tell a duplicate settle apart from an outage. Inside the settle
        # transaction the loser's insert first waits on the index until the
        # winner's transaction ends: commit yields this violation, rollback
        # lets the insert through and the loser settles instead.
        if _is_unique_violation(exc):
            logger.warning(
                "gate_attested_settle: lost the settle race for run %s — "
                "already claimed by a concurrent settle, refusing (no credit)",
                run_id,
            )
            return await _refuse(
                [CHECK_ALREADY_SETTLED], digest=digest, charter=charter
            )
        logger.exception(
            "gate_attested_settle: settled-audit write FAILED for run %s — "
            "refusing settle (no audit row, no credit)",
            run_id,
        )
        return await _refuse([CHECK_AUDIT_WRITE], digest=digest, charter=charter)
    logger.info("gate_attested_settle: SETTLED run=%s digest=%s", run_id, digest[:12])
    return {"outcome": "settled", "failed_checks": [], "envelope_digest": digest}


class SettlementGateObserver:
    """Post-seal settle trigger (Story 2.1 AC4, AR-18 Option A).

    Registered AFTER ``RunAttestationObserver`` in the boot composite. At run
    completion the attestation observer has already sealed the run (its
    in-memory ``envelopes`` map is populated before the DB persist attempt),
    so this observer resolves the just-sealed ``run_id`` from that reference
    — never from a blind newest-row query — and replays the deferred
    per-call settle through the gated path exactly once.
    """

    def __init__(self, attestation_observer: Any) -> None:
        self._attestation_observer = attestation_observer

    async def on_run_complete(self, session_id: str) -> None:
        try:
            from ..config import settings  # noqa: PLC0415
            from .settlement import (  # noqa: PLC0415
                pop_deferred_settle,
                settle_invocation,
            )

            # Pop first, THEN check the flag: a mid-process flip-off flushes
            # the deferred entry instead of leaking it (review finding, 2.1).
            kwargs = pop_deferred_settle(session_id)
            if not settings.settlement_require_attestation:
                if kwargs is not None:
                    logger.info(
                        "SettlementGateObserver: flag off — flushed deferred "
                        "entry for session %s",
                        session_id,
                    )
                return
            if kwargs is None:
                # The defer write runs inside asyncio.create_task; a fast run
                # can complete before that task executes. Bounded re-check
                # before concluding nothing was charged (review finding, 2.1).
                for _ in range(DEFER_RECHECKS):
                    await asyncio.sleep(DEFER_RECHECK_DELAY_S)
                    kwargs = pop_deferred_settle(session_id)
                    if kwargs is not None:
                        break
                if kwargs is None:
                    return
            run_id = self._sealed_run_id(session_id)
            if run_id is None:
                logger.warning(
                    "SettlementGateObserver: no sealed envelope for session %s — "
                    "deferred settle dropped (fail-closed)",
                    session_id,
                )
                return
            await settle_invocation(run_id=run_id, **kwargs)
        except Exception:
            logger.exception(
                "SettlementGateObserver: gated settle failed for session %s",
                session_id,
            )

    async def discard_run(self, session_id: str) -> None:
        """Drop the deferred entry for an abandoned run (review finding, 2.1).

        Dispatched via ``emit_run_discarded`` (stream error / cancel paths in
        the runner). Without this, run 1's deferred fee would sit in the map
        until run 2 on the same session seals — and settle against run 2's
        envelope. Mirrors ``RunAttestationObserver.discard_run``.
        """
        from .settlement import pop_deferred_settle  # noqa: PLC0415

        if pop_deferred_settle(session_id) is not None:
            logger.info(
                "SettlementGateObserver: discarded deferred settle for "
                "abandoned session %s",
                session_id,
            )

    def _sealed_run_id(self, session_id: str) -> str | None:
        """Resolve the just-sealed run from the attestation observer.

        Reads (and pops) the ``last_sealed`` binding the attestation observer
        records at seal time — an explicit session→run_id handoff, replacing
        the run-id prefix heuristic the review rejected (cross-session
        collision, format coupling to another package's private contract,
        eviction gap).
        """
        last_sealed = getattr(self._attestation_observer, "last_sealed", None)
        if not isinstance(last_sealed, dict):
            return None
        return last_sealed.pop(session_id, None)
