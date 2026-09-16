"""Ed25519-signed case attestations (KY-A supervisory harness, WS9 / FR-9.1).

Signs a session's case payload (e.g. the audit-ledger rationale trail from
``get_session_trail``) with the attestation key, and persists an
``attestations`` row with status ``pending``. The signature is over the
sha256 hex digest (``case_hash``) of the canonical payload, so anyone can
verify offline from ``payload`` + ``signature`` + ``public_key`` alone.

Canonicalisation matches ``emerge_node.envelope`` (sorted keys, compact
separators) and signature verification reuses ``envelope.verify_bytes`` —
one crypto implementation, compatible signatures.

Key management (FR-9.4, mock-first): the signing key comes from the env var
``ATTESTATION_PRIVATE_KEY_B64`` (base64 32-byte Ed25519 seed). If unset, an
ephemeral keypair is generated only when ``ATTESTATION_ALLOW_EPHEMERAL_KEY=1``
(dev/test). Otherwise the process refuses — at startup for a receipt-issuing
deployment, and at first seal otherwise. Private keys are NEVER written to
disk or git.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, NoReturn

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from emerge_node.envelope import generate_keypair, verify_bytes

logger = logging.getLogger(__name__)

PRIVATE_KEY_ENV = "ATTESTATION_PRIVATE_KEY_B64"
ALLOW_EPHEMERAL_KEY_ENV = "ATTESTATION_ALLOW_EPHEMERAL_KEY"

# Process-wide signing key, loaded lazily on first use (service start).
_signing_key: tuple[Ed25519PrivateKey, str] | None = None


class EphemeralAttestationKeyRefused(RuntimeError):
    """Named: receipts cannot be issued from an un-opted-in ephemeral key."""


MINT_SEED_ONELINER = (
    'python -c "from emerge_node.envelope import generate_keypair; '
    "from cryptography.hazmat.primitives.serialization import "
    "Encoding, PrivateFormat, NoEncryption; import base64; "
    "k,_=generate_keypair(); print(base64.b64encode(k.private_bytes("
    'Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode())"'
)


def _ephemeral_key_allowed() -> bool:
    return os.environ.get(ALLOW_EPHEMERAL_KEY_ENV, "").strip() == "1"


def _refuse_ephemeral() -> NoReturn:
    raise EphemeralAttestationKeyRefused(
        "ATTESTATION_PRIVATE_KEY_B64 is unset. A deployment that issues "
        "receipts needs a persistent seed, or ATTESTATION_ALLOW_EPHEMERAL_KEY=1 "
        "(dev/test only — receipts will not verify across restarts). Mint a "
        f"seed with: {MINT_SEED_ONELINER}"
    )


def require_signing_key_for_receipts() -> None:
    """Startup guard: refuse to boot a receipt issuer on an ephemeral key."""
    raw = os.environ.get(PRIVATE_KEY_ENV, "").strip()
    if raw:
        get_signing_key()
        return
    if _ephemeral_key_allowed():
        logger.warning(
            "%s=1 — ephemeral attestation key allowed (dev/test only)",
            ALLOW_EPHEMERAL_KEY_ENV,
        )
        return
    _refuse_ephemeral()


def _canonical_case_bytes(case_payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes of the case payload (same scheme as the envelope)."""

    def _default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    return json.dumps(
        case_payload, sort_keys=True, separators=(",", ":"), default=_default
    ).encode("utf-8")


def compute_case_hash(case_payload: dict[str, Any]) -> str:
    """sha256 hex digest of the canonical case payload."""
    return hashlib.sha256(_canonical_case_bytes(case_payload)).hexdigest()


def get_signing_key() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, public_key_b64), loading or generating on first use.

    Env var ``ATTESTATION_PRIVATE_KEY_B64`` holds a base64 32-byte seed. When
    absent, an ephemeral keypair is generated only if
    ``ATTESTATION_ALLOW_EPHEMERAL_KEY=1``; otherwise this raises
    :class:`EphemeralAttestationKeyRefused`. The key is never persisted.
    """
    global _signing_key
    if _signing_key is not None:
        return _signing_key

    raw = os.environ.get(PRIVATE_KEY_ENV, "").strip()
    if raw:
        seed = base64.b64decode(raw)
        if len(seed) != 32:
            raise ValueError(
                f"{PRIVATE_KEY_ENV} must be a base64 32-byte Ed25519 seed "
                f"(got {len(seed)} bytes)"
            )
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        public_key_b64 = base64.b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode("ascii")
        logger.info("Attestation signing key loaded from %s", PRIVATE_KEY_ENV)
    elif _ephemeral_key_allowed():
        logger.warning(
            "%s not set — generating an EPHEMERAL attestation keypair in memory. "
            "Signatures will not verify across restarts (dev/test only).",
            PRIVATE_KEY_ENV,
        )
        private_key, public_key_b64 = generate_keypair()
    else:
        _refuse_ephemeral()

    _signing_key = (private_key, public_key_b64)
    return _signing_key


def _reset_signing_key_for_tests() -> None:
    global _signing_key
    _signing_key = None


def verify_attestation(
    payload: dict[str, Any], signature_b64: str, public_key_b64: str
) -> bool:
    """Offline verification: recompute case_hash from payload and verify."""
    case_hash = compute_case_hash(payload)
    return verify_bytes(case_hash.encode("utf-8"), signature_b64, public_key_b64)


async def _ensure_db(db: Any) -> tuple[Any, bool]:
    """Return (db, owns_db). Lazily create a Prisma client when db is None."""
    if db is not None:
        return db, False
    from src.generated_client import Prisma

    client = Prisma()
    await client.connect()
    return client, True


def _prisma_json(value: Any) -> Any:
    """Wrap a dict for the Prisma JSON field; fall back to raw (fake dbs)."""
    try:
        from src.generated_client.fields import Json as PrismaJson

        return PrismaJson(value)
    except ImportError:
        return value


async def sign_case_attestation(
    session_id: str, case_payload: dict[str, Any], db: Any = None
) -> dict[str, Any]:
    """Sign a case payload and persist an attestation row (status ``pending``).

    Returns {attestation_id, case_hash, signature, public_key, status}.
    """
    case_hash = compute_case_hash(case_payload)
    private_key, public_key_b64 = get_signing_key()
    signature_b64 = base64.b64encode(
        private_key.sign(case_hash.encode("utf-8"))
    ).decode("ascii")

    client, owns_db = await _ensure_db(db)
    try:
        row = await client.attestation.create(
            data={
                "session_id": session_id,
                "case_hash": case_hash,
                "payload": _prisma_json(case_payload),
                "signature": signature_b64,
                "public_key": public_key_b64,
                "status": "pending",
            }
        )
    finally:
        if owns_db:
            await client.disconnect()

    logger.info(
        "Signed case attestation id=%s session=%s case_hash=%s",
        row.id,
        session_id,
        case_hash,
    )
    return {
        "attestation_id": row.id,
        "case_hash": case_hash,
        "signature": signature_b64,
        "public_key": public_key_b64,
        "status": "pending",
    }


# Retained references to fire-and-forget anchor tasks (prevents GC mid-flight).
_background_tasks: set[asyncio.Task[Any]] = set()


async def finalize_case(
    session_id: str, case_payload: dict[str, Any], db: Any = None
) -> dict[str, Any]:
    """KY-A wiring seam: sign the case attestation, then fire the testnet
    anchor as an asyncio background task (never inline in a user-facing path).

    Returns the same dict as ``sign_case_attestation``; anchoring proceeds
    asynchronously and updates the row to ``anchored`` or ``skipped``.
    """
    from .anchor import anchor_attestation

    result = await sign_case_attestation(session_id, case_payload, db=db)
    task = asyncio.create_task(anchor_attestation(result["attestation_id"], db=db))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return result
