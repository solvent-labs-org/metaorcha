"""Offline verifier for RFC 0003 run attestation envelopes.

Vendored, self-contained implementation of the ``orcha.run-attestation/v1``
verification algorithm (``docs/spec/rfcs/0003-run-attestation-envelope.md``).
It duplicates the byte-exact logic of ``validator.run_envelope`` /
``emerge_node.envelope`` on purpose: the SDK ships to verifiers who must be
able to check an attestation with zero platform services running, so the SDK
depends only on ``cryptography`` (the same Ed25519 library used by
``emerge_node``) plus the standard library.

Verification is fully offline: schema check, step hash-chain recomputation,
both step-commitment comparisons, and Ed25519 signature verification against the
envelope's embedded ``signer.public_key_b64``. DID resolution is never
required (RFC step 6 is optional).

The schema check enforces the RFC's charset rule: every free-text string in
the envelope is printable ASCII (``[\x20-\x7e]``) and every number is an
integer. That is what makes the envelope's canonical bytes identical under
RFC 8785 (JCS) and under the Python reference rendering — see the RFC's
"Canonical form and RFC 8785".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

RUN_ATTESTATION_FORMAT = "orcha.run-attestation/v1"

# Envelope-level: general DID syntax under the charset rule. The did:orcha
# method restriction is the platform profile, enforced by the platform, not
# by the schema (RFC 0003, "Platform profile").
_DID_RE = re.compile(r"^did:[a-z0-9]+:[\x20-\x7e]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Free-text fields: printable ASCII, non-empty (RFC 0003 charset rule).
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]+$")
# ``verdicts[].detail`` may be empty; it is still printable ASCII.
_PRINTABLE_ASCII_OR_EMPTY_RE = re.compile(r"^[\x20-\x7e]*$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# ``cdv_bp`` is a score in basis points: an integer in [0, 1000].
CDV_BP_MAX = 1000

_ENVELOPE_FIELDS = frozenset(
    {
        "format",
        "run_id",
        "agent_dids",
        "charter_hash",
        "policy_version",
        "steps",
        "steps_root",
        "steps_merkle_root",
        "verdicts",
        "started_at",
        "finished_at",
        "signer",
        "signature",
    }
)
_STEP_REQUIRED = frozenset(
    {"seq", "call_id", "tool", "args_hash", "output_hash", "success", "latency_ms"}
)
_STEP_ALLOWED = _STEP_REQUIRED | {"cdv_bp"}
_VERDICT_REQUIRED = frozenset({"check", "result"})
_VERDICT_ALLOWED = _VERDICT_REQUIRED | {"detail"}
_VERDICT_RESULTS = frozenset({"pass", "fail", "warn"})
_SIGNER_FIELDS = frozenset({"did", "public_key_b64"})


@dataclass(frozen=True)
class AttestationVerdict:
    """Structured result of an offline run attestation verification."""

    valid: bool
    checks: dict[str, bool] = field(
        default_factory=lambda: {
            "schema": False,
            "steps_root": False,
            "steps_merkle_root": False,
            "signature": False,
        }
    )
    run_id: str | None = None
    signer_did: str | None = None
    step_count: int | None = None

    @property
    def verdict(self) -> str:
        return "valid" if self.valid else "invalid"


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON bytes — the RFC's normative canonical form.

    Byte-identical to ``emerge_node.envelope.canonical_json_bytes``: object
    keys sorted, compact separators, non-ASCII escaped as ``\\uXXXX`` (Python
    ``json.dumps`` defaults).
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 digest of *data* (64 hex chars)."""
    return hashlib.sha256(data).hexdigest()


def compute_steps_root(steps: list[dict[str, Any]]) -> str:
    """Linear chain root over *hashed* step entries, per the RFC byte rule.

    ``h_0 = sha256(s_0)``; ``h_i = sha256(h_{i-1} || s_i)`` where ``h_{i-1}``
    is the **32 raw bytes** of the previous digest — never its hexadecimal
    rendering — and ``s_i = canonical_json_bytes(steps[i])``. Empty run →
    ``sha256_hex(b"")``. The returned value is the lowercase hex rendering of
    ``h_{n-1}``.
    """
    if not steps:
        return sha256_hex(b"")
    previous = b""
    for index, step in enumerate(steps):
        s_i = canonical_json_bytes(step)
        material = s_i if index == 0 else previous + s_i
        previous = hashlib.sha256(material).digest()
    return previous.hex()


def compute_steps_merkle_root(steps: list[dict[str, Any]]) -> str:
    """RFC 6962 §2.1 Merkle Tree Hash over the same canonical step bytes.

    Leaves are domain-separated with ``0x00`` and internal nodes with ``0x01``;
    the split is the largest power of two **strictly less than** ``n``. Both
    rules are normative — the alternative convention (duplicating a lone
    right-hand node) admits two distinct step lists with one root
    (CVE-2012-2459). Empty run → ``sha256_hex(b"")``, numerically identical to
    the empty-run ``steps_root`` by coincidence of the empty case only.
    """
    leaves = [canonical_json_bytes(step) for step in steps]

    def mth(data: list[bytes]) -> bytes:
        if not data:
            return hashlib.sha256(b"").digest()
        if len(data) == 1:
            return hashlib.sha256(b"\x00" + data[0]).digest()
        split = 1
        while split * 2 < len(data):
            split *= 2
        return hashlib.sha256(b"\x01" + mth(data[:split]) + mth(data[split:])).digest()

    return mth(leaves).hex()


def compute_envelope_digest(envelope: dict[str, Any]) -> str:
    """sha256 hex of the canonical envelope with ``signature`` stripped."""
    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
    return sha256_hex(canonical_json_bytes(unsigned))


def _verify_signature(message: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Ed25519 verification — same call path as emerge_node.verify_bytes."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64)
        )
        public_key.verify(base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _schema_ok(envelope: Any) -> bool:
    """RFC verification step 1 — structural/schema check (closed objects)."""
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        return False
    if envelope["format"] != RUN_ATTESTATION_FORMAT:
        return False
    if not isinstance(envelope["run_id"], str) or not _PRINTABLE_ASCII_RE.match(
        envelope["run_id"]
    ):
        return False
    agent_dids = envelope["agent_dids"]
    if not isinstance(agent_dids, list) or not all(
        isinstance(d, str) and _DID_RE.match(d) for d in agent_dids
    ):
        return False
    charter_hash = envelope["charter_hash"]
    if charter_hash is not None and not (
        isinstance(charter_hash, str) and _HEX64_RE.match(charter_hash)
    ):
        return False
    if not isinstance(envelope["policy_version"], str) or not (
        _PRINTABLE_ASCII_RE.match(envelope["policy_version"])
    ):
        return False
    if not (
        isinstance(envelope["steps_root"], str)
        and _HEX64_RE.match(envelope["steps_root"])
    ):
        return False
    if not (
        isinstance(envelope["steps_merkle_root"], str)
        and _HEX64_RE.match(envelope["steps_merkle_root"])
    ):
        return False
    if not _RFC3339_Z_RE.match(str(envelope["started_at"])) or not _RFC3339_Z_RE.match(
        str(envelope["finished_at"])
    ):
        return False

    steps = envelope["steps"]
    if not isinstance(steps, list):
        return False
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not set(step) <= set(_STEP_ALLOWED):
            return False
        if not set(step) >= set(_STEP_REQUIRED):
            return False
        if step["seq"] != index or not _is_int(step["seq"]):
            return False
        if not isinstance(step["call_id"], str) or not _PRINTABLE_ASCII_RE.match(
            step["call_id"]
        ):
            return False
        if not isinstance(step["tool"], str) or not _PRINTABLE_ASCII_RE.match(
            step["tool"]
        ):
            return False
        if not (
            isinstance(step["args_hash"], str) and _HEX64_RE.match(step["args_hash"])
        ):
            return False
        if not (
            isinstance(step["output_hash"], str)
            and _HEX64_RE.match(step["output_hash"])
        ):
            return False
        if not isinstance(step["success"], bool):
            return False
        if not _is_int(step["latency_ms"]) or step["latency_ms"] < 0:
            return False
        if "cdv_bp" in step:
            cdv_bp = step["cdv_bp"]
            if not _is_int(cdv_bp) or not 0 <= cdv_bp <= CDV_BP_MAX:
                return False

    verdicts = envelope["verdicts"]
    if not isinstance(verdicts, list):
        return False
    for verdict in verdicts:
        if not isinstance(verdict, dict) or not set(verdict) <= set(_VERDICT_ALLOWED):
            return False
        if not set(verdict) >= set(_VERDICT_REQUIRED):
            return False
        if not isinstance(verdict["check"], str) or not _PRINTABLE_ASCII_RE.match(
            verdict["check"]
        ):
            return False
        if verdict["result"] not in _VERDICT_RESULTS:
            return False
        if "detail" in verdict and (
            not isinstance(verdict["detail"], str)
            or not _PRINTABLE_ASCII_OR_EMPTY_RE.match(verdict["detail"])
        ):
            return False

    signer_obj = envelope["signer"]
    if not isinstance(signer_obj, dict) or set(signer_obj) != _SIGNER_FIELDS:
        return False
    if not (isinstance(signer_obj["did"], str) and _DID_RE.match(signer_obj["did"])):
        return False
    if not isinstance(signer_obj["public_key_b64"], str) or not _BASE64_RE.match(
        signer_obj["public_key_b64"]
    ):
        return False
    return isinstance(envelope["signature"], str) and bool(
        _BASE64_RE.match(envelope["signature"])
    )


def _extract_display_fields(
    envelope: Any,
) -> tuple[str | None, str | None, int | None]:
    """Best-effort run_id / signer DID / step count for reporting purposes."""
    if not isinstance(envelope, dict):
        return None, None, None
    run_id = envelope.get("run_id")
    signer = envelope.get("signer")
    steps = envelope.get("steps")
    return (
        run_id if isinstance(run_id, str) else None,
        signer.get("did")
        if isinstance(signer, dict) and isinstance(signer.get("did"), str)
        else None,
        len(steps) if isinstance(steps, list) else None,
    )


def verify_run_attestation(envelope: Any) -> AttestationVerdict:
    """RFC 0003 verification algorithm, fully offline.

    1. Schema check (format, closed field sets, DID patterns, hex fields,
       gapless 0-based ``seq``).
    2./3. Recompute both step commitments and compare ``steps_root`` and
       ``steps_merkle_root``. A mismatch in either is a hard failure.
    4. Recompute the digest of the unsigned envelope.
    5. Verify the Ed25519 signature over the digest against the envelope's
       embedded ``signer.public_key_b64``.

    Returns an :class:`AttestationVerdict`; ``valid`` is True only when every
    check passes (no partial-trust states in v1).
    """
    run_id, signer_did, step_count = _extract_display_fields(envelope)
    checks = {
        "schema": False,
        "steps_root": False,
        "steps_merkle_root": False,
        "signature": False,
    }
    try:
        if not _schema_ok(envelope):
            return AttestationVerdict(False, checks, run_id, signer_did, step_count)
        checks["schema"] = True
        if compute_steps_root(envelope["steps"]) != envelope["steps_root"]:
            return AttestationVerdict(False, checks, run_id, signer_did, step_count)
        checks["steps_root"] = True
        if (
            compute_steps_merkle_root(envelope["steps"])
            != envelope["steps_merkle_root"]
        ):
            return AttestationVerdict(False, checks, run_id, signer_did, step_count)
        checks["steps_merkle_root"] = True
        digest = compute_envelope_digest(envelope)
        if not _verify_signature(
            digest.encode("utf-8"),
            envelope["signature"],
            envelope["signer"]["public_key_b64"],
        ):
            return AttestationVerdict(False, checks, run_id, signer_did, step_count)
        checks["signature"] = True
        return AttestationVerdict(True, checks, run_id, signer_did, step_count)
    except Exception:  # defensive: any malformed input is simply invalid
        logger.exception(
            "verify_run_attestation: unexpected error; treating as invalid"
        )
        return AttestationVerdict(False, checks, run_id, signer_did, step_count)
