"""Run attestation envelopes (RFC 0003 — ``orcha.run-attestation/v1``), platform side.

The envelope is built, signed and verified by the published SDK and nowhere
else (AD-5 for the verifier, AD-20 for the producer): :mod:`emerge.record`
hashes the steps, assembles the envelope and signs its digest;
:mod:`emerge.run_attestation` holds the canonical bytes, both step
commitments, the digest and the verifier, pinned to the shared golden vector
``docs/spec/test-vectors/run-attestation-golden.json``. A plugin receipt and
a platform receipt for the same run are the same bytes because they are made
by the same code.

What is platform-only lives here: the process-wide signing key
(``signer.get_signing_key``: ``ATTESTATION_PRIVATE_KEY_B64``, or an ephemeral
keypair under ``ATTESTATION_ALLOW_EPHEMERAL_KEY=1`` — the SDK's local producer
never reads that variable), the CDV score rendering, and the
``attestations`` table. The names the rest of the platform imports from this
module are re-exported unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge.record import build_run_envelope as _build_run_envelope
from emerge.record import sign_run_envelope as _sign_run_envelope
from emerge.run_attestation import (  # noqa: F401 — re-exported platform names
    CDV_BP_MAX,
    RUN_ATTESTATION_FORMAT,
    canonical_json_bytes,
    compute_envelope_digest,
    compute_steps_merkle_root,
    compute_steps_root,
    sha256_hex,
    verify_run_attestation,
)

from .signer import _ensure_db, _prisma_json, get_signing_key

# the re-exported names are this module's public API too (readers import them here)
__all__ = [
    "CDV_BP_MAX",
    "RUN_ATTESTATION_FORMAT",
    "build_run_envelope",
    "canonical_json_bytes",
    "cdv_score_to_bp",
    "compute_envelope_digest",
    "compute_steps_merkle_root",
    "compute_steps_root",
    "get_latest_run_attestation_for_session",
    "get_run_attestation_by_run_id",
    "get_run_attestation_record",
    "persist_run_attestation",
    "sha256_hex",
    "sign_run_envelope",
    "verify_run_attestation",
    "verify_run_envelope",
]

logger = logging.getLogger(__name__)


def cdv_score_to_bp(score: float) -> int:
    """Render a CDV score in ``[0.0, 1.0]`` as basis points ``[0, 1000]``.

    The envelope carries integers only (RFC 0003 charset rule), so the
    ``cdv`` package's float is rounded to the nearest basis point at the
    producer and clamped into range.
    """
    return max(0, min(CDV_BP_MAX, round(float(score) * CDV_BP_MAX)))


def build_run_envelope(
    *,
    run_id: str,
    agent_dids: list[str],
    charter_hash: str | None,
    policy_version: str,
    steps: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
    signer_did: str,
    public_key_b64: str | None = None,
) -> dict[str, Any]:
    """Build an unsigned envelope with the SDK producer (:func:`emerge.record.build_run_envelope`).

    *steps* are raw entries ``{call_id, tool, args, output, success,
    latency_ms, cdv_bp?}``; they are hashed per the RFC (raw content never
    lands in the envelope) and ``seq`` is assigned by position. When
    *public_key_b64* is omitted the process-wide attestation key is named.

    Raises ValueError on any input that cannot produce a conformant envelope.
    """
    if public_key_b64 is None:
        _, public_key_b64 = get_signing_key()
    return _build_run_envelope(
        run_id=run_id,
        agent_dids=agent_dids,
        charter_hash=charter_hash,
        policy_version=policy_version,
        steps=steps,
        verdicts=verdicts,
        started_at=started_at,
        finished_at=finished_at,
        signer_did=signer_did,
        public_key_b64=public_key_b64,
    )


def sign_run_envelope(
    envelope: dict[str, Any], private_key: Ed25519PrivateKey | None = None
) -> dict[str, Any]:
    """Sign with the SDK producer; the process-wide key when none is given.

    Returns a new dict; the input is not mutated.
    """
    if private_key is None:
        private_key, _ = get_signing_key()
    return _sign_run_envelope(envelope, private_key)


def verify_run_envelope(envelope: dict[str, Any]) -> bool:
    """The one verifier's answer, as a boolean. False on any failure.

    :func:`emerge.run_attestation.verify_run_attestation` runs the RFC 0003
    algorithm (schema, both step commitments, digest, signature against the
    envelope's own signer key). Trust anchors are the gate's concern
    (``settle_gate``), not the verifier's.
    """
    try:
        return bool(verify_run_attestation(envelope).valid)
    except Exception:  # defensive: any malformed input is simply invalid
        logger.exception("verify_run_envelope: unexpected error; treating as invalid")
        return False


async def persist_run_attestation(
    session_id: str, envelope: dict[str, Any], db: Any = None
) -> dict[str, Any] | None:
    """Persist a signed envelope as an ``attestations`` row (status ``pending``).

    Field mapping: ``session_id`` ← the run's session id, ``run_id`` ← the
    envelope's RFC 0003 ``run_id``, ``case_hash`` ← the signed envelope
    digest, ``payload`` ← the full envelope JSON,
    ``signature``/``public_key`` ← the envelope's. Returns the row summary, or
    None when the DB is unavailable — an attestation persistence failure must
    never crash a run.
    """
    try:
        client, owns_db = await _ensure_db(db)
        try:
            row = await client.attestation.create(
                data={
                    "session_id": session_id,
                    "run_id": envelope["run_id"],
                    "case_hash": compute_envelope_digest(envelope),
                    "payload": _prisma_json(envelope),
                    "signature": envelope["signature"],
                    "public_key": envelope["signer"]["public_key_b64"],
                    "status": "pending",
                }
            )
        finally:
            if owns_db:
                await client.disconnect()
    except Exception:
        logger.warning(
            "persist_run_attestation: DB unavailable for session %s — envelope "
            "kept in memory only",
            session_id,
            exc_info=True,
        )
        return None
    logger.info(
        "Persisted run attestation id=%s session=%s digest=%s",
        row.id,
        session_id,
        row.case_hash,
    )
    return {
        "attestation_id": row.id,
        "run_id": row.run_id,
        "case_hash": row.case_hash,
        "signature": row.signature,
        "public_key": row.public_key,
        "status": "pending",
    }


def _envelope_payload(row: Any, *, lookup: str) -> dict[str, Any] | None:
    """Unwrap a stored payload. Corrupt / non-dict collapses to None (AD-5)."""
    payload = getattr(row.payload, "data", row.payload)
    if not isinstance(payload, dict):
        logger.warning(
            "%s: corrupt payload — treating as missing",
            lookup,
        )
        return None
    return payload


async def get_run_attestation_record(
    run_id: str, db: Any = None
) -> dict[str, Any] | None:
    """Load ``{session_id, run_id, envelope}`` by RFC 0003 ``run_id``.

    Same miss/unavailable/corrupt contract as
    :func:`get_run_attestation_by_run_id` — never raises, never verifies.
    The HTTP fetch route uses ``session_id`` for the ownership check; the
    settle gate still wants the bare envelope.
    """
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        client, owns_db = await _ensure_db(db)
        try:
            row = await client.attestation.find_unique(where={"run_id": run_id})
        finally:
            if owns_db:
                await client.disconnect()
    except Exception:
        logger.warning(
            "get_run_attestation_record: DB unavailable looking up run %s",
            run_id,
            exc_info=True,
        )
        return None
    if row is None:
        logger.info("get_run_attestation_record: no attestation for run %s", run_id)
        return None
    envelope = _envelope_payload(row, lookup=f"get_run_attestation_record run {run_id}")
    if envelope is None:
        return None
    return {
        "session_id": row.session_id,
        "run_id": row.run_id,
        "envelope": envelope,
    }


async def get_run_attestation_by_run_id(
    run_id: str, db: Any = None
) -> dict[str, Any] | None:
    """Load a persisted run attestation envelope by its RFC 0003 ``run_id``.

    Returns the stored envelope payload dict, or ``None`` when no row exists
    for ``run_id`` (deterministic not-found), when the DB is unavailable, or
    when the stored payload is corrupt (not a dict) — this helper never
    raises. Gate callers (AD-4/AD-10) treat any ``None`` as a missing
    attestation and refuse (fail-closed, AD-6); the ``None`` cases are
    distinguishable only in the logs. The payload is returned stored-as-is —
    verification is the caller's job (AD-5).
    """
    record = await get_run_attestation_record(run_id, db=db)
    return None if record is None else record["envelope"]


async def get_latest_run_attestation_for_session(
    session_id: str, db: Any = None
) -> dict[str, Any] | None:
    """Latest persisted envelope for ``session_id`` (``created_at`` desc).

    Same miss/unavailable/corrupt contract as
    :func:`get_run_attestation_record` — never raises, never verifies. A
    session may accumulate more than one sealed turn; the fetch route serves
    the newest.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        client, owns_db = await _ensure_db(db)
        try:
            rows = await client.attestation.find_many(
                where={"session_id": session_id},
                order={"created_at": "desc"},
            )
        finally:
            if owns_db:
                await client.disconnect()
    except Exception:
        logger.warning(
            "get_latest_run_attestation_for_session: DB unavailable "
            "looking up session %s",
            session_id,
            exc_info=True,
        )
        return None
    if not rows:
        logger.info(
            "get_latest_run_attestation_for_session: no attestation for session %s",
            session_id,
        )
        return None
    row = rows[0]
    envelope = _envelope_payload(
        row, lookup=f"get_latest_run_attestation_for_session {session_id}"
    )
    if envelope is None:
        return None
    return {
        "session_id": row.session_id,
        "run_id": row.run_id,
        "envelope": envelope,
    }
