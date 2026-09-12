"""Run attestation envelopes (RFC 0003 — ``orcha.run-attestation/v1``).

NOTE: this verifier is paired with the vendored copy in the SDK at
``sdk/src/emerge/run_attestation.py``, and both are pinned by the shared
test vector ``docs/spec/test-vectors/run-attestation-golden.json``. Any
format change must land in both implementations and the vector together.

Builds, signs, and verifies signed run attestation envelopes: a run's ordered
tool-call transcript (as hashes), verdicts, and timing, bound to an Ed25519
signing key and verifiable offline for free.

Conformance-critical rules (byte-exact; two implementers MUST produce
byte-identical output):

- Canonicalisation reuses ``emerge_node.envelope.canonical_json_bytes``
  verbatim (sorted keys, compact separators, ``\\uXXXX`` escaping) — it is the
  RFC's normative reference and is never reimplemented here.
- ``args_hash`` / ``output_hash`` are ``sha256_hex(canonical_json_bytes(...))``
  of the raw args/output. Raw content is NEVER stored in the envelope
  (privacy by design).
- Two commitments over the same ``s_i = canonical_json_bytes(steps[i])``,
  both required and both checked. Chain: ``h_0 = sha256(s_0)``; ``h_i =
  sha256(h_{i-1} || s_i)`` over the **32 raw** digest bytes, never their hex
  rendering. Tree: RFC 6962 §2.1 Merkle Tree Hash, leaves prefixed ``0x00``,
  nodes ``0x01``, split at the largest power of two strictly below ``n``.
  Empty run → ``sha256_hex(b"")`` for both.
- Signing follows the AAC charter convention (``charter.signing``): Ed25519
  over the UTF-8 bytes of the lowercase hex sha256 digest of the canonical
  unsigned envelope — the signature is over the hex digest, not the bytes.
- Charset rule: every free-text string (``run_id``, ``policy_version``,
  ``call_id``, ``tool``, ``verdicts[].check``/``detail``, the DIDs) is
  printable ASCII ``[\x20-\x7e]`` and every number is an integer —
  ``cdv_bp`` carries the CDV score in basis points, ``0``–``1000``. Under that
  rule the canonical bytes are identical under RFC 8785 (JCS) and under the
  Python reference rendering, so the chain, the Merkle root, the digest and
  the signature can be recomputed by a JCS implementation in any language.

Key handling mirrors ``signer.py`` (FR-9.4, mock-first): the signing key comes
from ``ATTESTATION_PRIVATE_KEY_B64`` (base64 32-byte Ed25519 seed); when unset,
an ephemeral in-memory keypair is generated with a loud warning.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge_node.envelope import canonical_json_bytes, verify_bytes

from .signer import _ensure_db, _prisma_json, get_signing_key

logger = logging.getLogger(__name__)

RUN_ATTESTATION_FORMAT = "orcha.run-attestation/v1"

# Envelope-level (RFC 0003 schema): general DID syntax under the charset rule.
_DID_RE = re.compile(r"^did:[a-z0-9]+:[\x20-\x7e]+$")
# Platform profile: what this producer emits and the settlement gate expects.
_PLATFORM_DID_RE = re.compile(r"^did:orcha:(agent|system):[\x20-\x7e]+$")
# Mirrored in superagent/config.py (charter-hash settings validator) — keep in sync.
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


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 digest of *data* (64 hex chars)."""
    return hashlib.sha256(data).hexdigest()


def _hash_value(value: Any) -> str:
    """sha256 hex of the canonical JSON of any JSON-serializable value."""
    return sha256_hex(canonical_json_bytes(value))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_did(did: Any, field: str) -> None:
    _require(
        isinstance(did, str) and bool(_PLATFORM_DID_RE.match(did)),
        f"{field} must match did:orcha:agent:* or did:orcha:system:* in printable "
        f"ASCII (got {did!r})",
    )


def _validate_text(value: Any, field: str, *, allow_empty: bool = False) -> None:
    """Free-text fields are printable ASCII (RFC 0003 charset rule)."""
    pattern = _PRINTABLE_ASCII_OR_EMPTY_RE if allow_empty else _PRINTABLE_ASCII_RE
    _require(
        isinstance(value, str) and bool(pattern.match(value)),
        f"{field} must be a {'' if allow_empty else 'non-empty '}printable-ASCII "
        f"string (got {value!r})",
    )


def _validate_timestamp(value: Any, field: str) -> None:
    _require(
        isinstance(value, str) and bool(_RFC3339_Z_RE.match(value)),
        f"{field} must be RFC 3339 UTC with Z designator (got {value!r})",
    )


def _canonical_step(
    seq: int,
    *,
    call_id: Any,
    tool: Any,
    args: Any,
    output: Any,
    success: Any,
    latency_ms: Any,
    cdv_bp: Any = None,
) -> dict[str, Any]:
    """Hash one raw step entry into its canonical envelope form."""
    _validate_text(call_id, "steps[].call_id")
    _validate_text(tool, "steps[].tool")
    _require(isinstance(args, dict), "steps[].args must be a JSON object")
    _require(isinstance(success, bool), "steps[].success must be a boolean")
    _require(
        isinstance(latency_ms, int)
        and not isinstance(latency_ms, bool)
        and latency_ms >= 0,
        "steps[].latency_ms must be an integer >= 0",
    )
    step: dict[str, Any] = {
        "seq": seq,
        "call_id": call_id,
        "tool": tool,
        "args_hash": _hash_value(args),
        "output_hash": _hash_value(output),
        "success": success,
        "latency_ms": latency_ms,
    }
    if cdv_bp is not None:
        _require(
            _is_int(cdv_bp) and 0 <= cdv_bp <= CDV_BP_MAX,
            f"steps[].cdv_bp must be an integer in [0, {CDV_BP_MAX}] (basis points)",
        )
        step["cdv_bp"] = cdv_bp
    return step


def cdv_score_to_bp(score: float) -> int:
    """Render a CDV score in ``[0.0, 1.0]`` as basis points ``[0, 1000]``.

    The envelope carries integers only (RFC 0003 charset rule), so the
    ``cdv`` package's float is rounded to the nearest basis point at the
    producer and clamped into range.
    """
    return max(0, min(CDV_BP_MAX, round(float(score) * CDV_BP_MAX)))


def _canonical_verdict(entry: Any) -> dict[str, Any]:
    _require(isinstance(entry, dict), "verdicts[] entries must be objects")
    _require(
        set(entry) <= set(_VERDICT_ALLOWED),
        f"verdicts[] allows only {sorted(_VERDICT_ALLOWED)}",
    )
    check = entry.get("check")
    result = entry.get("result")
    _validate_text(check, "verdicts[].check")
    _require(result in _VERDICT_RESULTS, "verdicts[].result must be pass|fail|warn")
    verdict: dict[str, Any] = {"check": check, "result": result}
    detail = entry.get("detail")
    if detail is not None:
        _validate_text(detail, "verdicts[].detail", allow_empty=True)
        verdict["detail"] = detail
    return verdict


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
    """Build an unsigned ``orcha.run-attestation/v1`` envelope from run data.

    *steps* are raw entries ``{call_id, tool, args, output, success,
    latency_ms, cdv_bp?}``; they are hashed per the RFC (raw content never
    lands in the envelope) and ``seq`` is assigned by position.

    When *public_key_b64* is omitted the process-wide attestation key is used
    (``ATTESTATION_PRIVATE_KEY_B64``, or an ephemeral keypair with a warning).

    Raises ValueError on any input that cannot produce a conformant envelope.
    """
    _validate_text(run_id, "run_id")
    _require(isinstance(agent_dids, list), "agent_dids must be a list")
    for did in agent_dids:
        _validate_did(did, "agent_dids")
    _require(
        charter_hash is None
        or (isinstance(charter_hash, str) and bool(_HEX64_RE.match(charter_hash))),
        "charter_hash must be None or 64 lowercase hex chars",
    )
    _validate_text(policy_version, "policy_version")
    _validate_timestamp(started_at, "started_at")
    _validate_timestamp(finished_at, "finished_at")
    _validate_did(signer_did, "signer_did")

    if public_key_b64 is None:
        _, public_key_b64 = get_signing_key()

    hashed_steps = [_canonical_step(seq, **raw) for seq, raw in enumerate(steps)]
    return {
        "format": RUN_ATTESTATION_FORMAT,
        "run_id": run_id,
        "agent_dids": list(agent_dids),
        "charter_hash": charter_hash,
        "policy_version": policy_version,
        "steps": hashed_steps,
        "steps_root": compute_steps_root(hashed_steps),
        "steps_merkle_root": compute_steps_merkle_root(hashed_steps),
        "verdicts": [_canonical_verdict(v) for v in verdicts],
        "started_at": started_at,
        "finished_at": finished_at,
        "signer": {"did": signer_did, "public_key_b64": public_key_b64},
    }


def compute_envelope_digest(envelope: dict[str, Any]) -> str:
    """sha256 hex of the canonical envelope with ``signature`` stripped."""
    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
    return sha256_hex(canonical_json_bytes(unsigned))


def sign_run_envelope(
    envelope: dict[str, Any], private_key: Ed25519PrivateKey | None = None
) -> dict[str, Any]:
    """Attach an Ed25519 signature over the hex envelope digest (charter rule).

    *envelope* is the unsigned envelope from :func:`build_run_envelope`.
    When *private_key* is omitted the process-wide attestation key signs.
    Returns a new dict; the input is not mutated.
    """
    if private_key is None:
        private_key, _ = get_signing_key()
    digest = compute_envelope_digest(envelope)
    signature_b64 = base64.b64encode(private_key.sign(digest.encode("utf-8"))).decode(
        "ascii"
    )
    return {**envelope, "signature": signature_b64}


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


def verify_run_envelope(
    envelope: dict[str, Any], public_key_b64: str | None = None
) -> bool:
    """RFC 0003 verification algorithm, fully offline. False on any failure.

    1. Schema check (format, closed field sets, DID patterns, hex fields,
       gapless 0-based ``seq``).
    2./3. Recompute both step commitments and compare ``steps_root`` and
       ``steps_merkle_root``. A mismatch in either is a hard failure.
    4. Recompute the digest of the unsigned envelope.
    5. Verify the Ed25519 signature over the digest against
       *public_key_b64* (defaults to the envelope's ``signer.public_key_b64``).
    """
    try:
        if not _schema_ok(envelope):
            return False
        if compute_steps_root(envelope["steps"]) != envelope["steps_root"]:
            return False
        if (
            compute_steps_merkle_root(envelope["steps"])
            != envelope["steps_merkle_root"]
        ):
            return False
        digest = compute_envelope_digest(envelope)
        key = public_key_b64 or envelope["signer"]["public_key_b64"]
        return verify_bytes(digest.encode("utf-8"), envelope["signature"], key)
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
