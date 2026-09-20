"""Local producer for RFC 0003 run attestation envelopes.

Pairs with the vendored verifier in :mod:`emerge.run_attestation`: this module
builds and signs ``orcha.run-attestation/v1`` envelopes with no platform
service, no database and no network — only ``cryptography`` and the standard
library. It is the byte-exact counterpart of the platform producer
(``services/validator/src/validator/run_envelope.py``); both are pinned to the
shared golden vector ``docs/spec/test-vectors/run-attestation-golden.json``,
and a divergence between them fails ``sdk/tests/test_record.py``.

Rules carried over unchanged (see the RFC and the verifier's docstring):

- Canonical bytes are Python ``json.dumps(sort_keys=True, separators=(",",
  ":"))`` — the reference rendering the RFC names. Under the charset rule
  they coincide with RFC 8785, but they are not reimplemented as JCS here.
- ``args_hash`` / ``output_hash`` are ``sha256_hex(canonical_json_bytes(...))``
  of the raw values; raw args and outputs never enter the envelope.
- The signature is Ed25519 over the UTF-8 bytes of the lowercase hex digest
  of the canonical unsigned envelope (the charter signing rule).
- Free text is printable ASCII, every number an integer; ``cdv_bp`` is an
  integer in ``[0, 1000]``. DIDs follow the platform profile
  (``did:orcha:agent:*`` / ``did:orcha:system:*``).

Key handling mirrors the platform's ``ATTESTATION_PRIVATE_KEY_B64`` semantics
(a base64 32-byte Ed25519 seed). For the CLI the seed may instead live in a
local key file (default ``~/.orcha/key``, mode ``0600``); the environment
variable wins when set. A receipt that is kept must never be signed with an
ephemeral key: with no seed in the environment and no key file, sealing is
refused unless ``ATTESTATION_ALLOW_EPHEMERAL_KEY=1`` opts in for dev/test.
Seeds are never printed or logged.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .run_attestation import (
    CDV_BP_MAX,
    RUN_ATTESTATION_FORMAT,
    canonical_json_bytes,
    compute_envelope_digest,
    compute_steps_merkle_root,
    compute_steps_root,
    sha256_hex,
)

PRIVATE_KEY_ENV = "ATTESTATION_PRIVATE_KEY_B64"
ALLOW_EPHEMERAL_KEY_ENV = "ATTESTATION_ALLOW_EPHEMERAL_KEY"
KEY_PATH_ENV = "ORCHA_KEY_PATH"
DEFAULT_KEY_PATH = Path("~/.orcha/key")

_PLATFORM_DID_RE = re.compile(r"^did:orcha:(agent|system):[\x20-\x7e]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]+$")
_PRINTABLE_ASCII_OR_EMPTY_RE = re.compile(r"^[\x20-\x7e]*$")

_VERDICT_ALLOWED = frozenset({"check", "result", "detail"})
_VERDICT_RESULTS = frozenset({"pass", "fail", "warn"})
_STEP_FIELDS = frozenset(
    {"call_id", "tool", "args", "output", "success", "latency_ms", "cdv_bp"}
)


class EphemeralKeyRefused(RuntimeError):
    """Named: a kept receipt cannot be signed by a key that will not persist."""


# ── Validation (the platform producer's guards, verbatim in effect) ──────────


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


# ── Hashing and canonical forms ──────────────────────────────────────────────


def hash_value(value: Any) -> str:
    """sha256 hex of the canonical JSON of any JSON-serializable value."""
    return sha256_hex(canonical_json_bytes(value))


def canonical_step(
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
    """Hash one raw step entry into its canonical envelope form.

    Field order is the envelope's rendering order; the canonical bytes sort
    keys anyway, so order matters only for the human-readable file.
    """
    _validate_text(call_id, "steps[].call_id")
    _validate_text(tool, "steps[].tool")
    _require(isinstance(args, dict), "steps[].args must be a JSON object")
    _require(isinstance(success, bool), "steps[].success must be a boolean")
    _require(
        _is_int(latency_ms) and latency_ms >= 0,
        "steps[].latency_ms must be an integer >= 0",
    )
    step: dict[str, Any] = {
        "seq": seq,
        "call_id": call_id,
        "tool": tool,
        "args_hash": hash_value(args),
        "output_hash": hash_value(output),
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


def canonical_verdict(entry: Any) -> dict[str, Any]:
    """Validate one verdict entry and return it in envelope form."""
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


def _raw_step_kwargs(raw: Any, index: int) -> dict[str, Any]:
    _require(isinstance(raw, dict), f"steps[{index}] must be an object")
    unknown = set(raw) - _STEP_FIELDS
    _require(
        not unknown,
        f"steps[{index}] has unknown fields {sorted(unknown)}; "
        f"allowed: {sorted(_STEP_FIELDS)}",
    )
    missing = {"call_id", "tool", "args", "output", "success", "latency_ms"} - set(raw)
    _require(not missing, f"steps[{index}] is missing {sorted(missing)}")
    return dict(raw)


# ── Build and sign ───────────────────────────────────────────────────────────


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
    public_key_b64: str,
) -> dict[str, Any]:
    """Build an unsigned ``orcha.run-attestation/v1`` envelope from run data.

    *steps* are raw entries ``{call_id, tool, args, output, success,
    latency_ms, cdv_bp?}``; they are hashed per the RFC (raw content never
    lands in the envelope) and ``seq`` is assigned by position. Unlike the
    platform producer there is no process-wide key: *public_key_b64* is
    required, so the envelope can only ever name a key the caller holds.

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
    _require(isinstance(steps, list), "steps must be a list")
    _require(isinstance(verdicts, list), "verdicts must be a list")
    _validate_timestamp(started_at, "started_at")
    _validate_timestamp(finished_at, "finished_at")
    _validate_did(signer_did, "signer_did")
    _require(
        isinstance(public_key_b64, str) and len(base64.b64decode(public_key_b64)) == 32,
        "public_key_b64 must be a base64 32-byte Ed25519 public key",
    )

    hashed_steps = [
        canonical_step(seq, **_raw_step_kwargs(raw, seq))
        for seq, raw in enumerate(steps)
    ]
    return {
        "format": RUN_ATTESTATION_FORMAT,
        "run_id": run_id,
        "agent_dids": list(agent_dids),
        "charter_hash": charter_hash,
        "policy_version": policy_version,
        "steps": hashed_steps,
        "steps_root": compute_steps_root(hashed_steps),
        "steps_merkle_root": compute_steps_merkle_root(hashed_steps),
        "verdicts": [canonical_verdict(v) for v in verdicts],
        "started_at": started_at,
        "finished_at": finished_at,
        "signer": {"did": signer_did, "public_key_b64": public_key_b64},
    }


def sign_run_envelope(
    envelope: dict[str, Any], private_key: Ed25519PrivateKey
) -> dict[str, Any]:
    """Attach an Ed25519 signature over the hex envelope digest (charter rule).

    Returns a new dict; the input is not mutated. The key is explicit —
    there is no ambient key in the SDK.
    """
    digest = compute_envelope_digest(envelope)
    signature_b64 = base64.b64encode(private_key.sign(digest.encode("utf-8"))).decode(
        "ascii"
    )
    return {**envelope, "signature": signature_b64}


# ── Keys ─────────────────────────────────────────────────────────────────────


def public_key_b64_of(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")


def key_did(private_key: Ed25519PrivateKey) -> str:
    """The default signer DID for a local key: ``did:orcha:agent:key-<fp>``.

    ``fp`` is the first 16 hex characters of the SHA-256 of the raw 32-byte
    public key. This is a naming convention for locally sealed receipts, not
    a new envelope field; any platform-profile DID may be passed instead.
    """
    raw = private_key.public_key().public_bytes_raw()
    return f"did:orcha:agent:key-{hashlib.sha256(raw).hexdigest()[:16]}"


def _seed_from_b64(raw: str, source: str) -> Ed25519PrivateKey:
    try:
        seed = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{source} is not valid base64") from exc
    if len(seed) != 32:
        raise ValueError(
            f"{source} must be a base64 32-byte Ed25519 seed (got {len(seed)} bytes)"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def resolve_key_path(key_path: str | os.PathLike[str] | None = None) -> Path:
    """The key file to use: explicit argument, then ``ORCHA_KEY_PATH``, then
    ``~/.orcha/key``."""
    if key_path is not None:
        return Path(key_path).expanduser()
    env = os.environ.get(KEY_PATH_ENV, "").strip()
    return Path(env).expanduser() if env else DEFAULT_KEY_PATH.expanduser()


def load_signing_key(
    key_path: str | os.PathLike[str] | None = None,
) -> Ed25519PrivateKey:
    """Load the local signing key.

    Order: ``ATTESTATION_PRIVATE_KEY_B64`` when set (same semantics as the
    platform), else the key file. With neither, an ephemeral key is generated
    only when ``ATTESTATION_ALLOW_EPHEMERAL_KEY=1``; otherwise
    :class:`EphemeralKeyRefused` is raised, because a receipt sealed by a key
    nobody keeps cannot be re-verified against that signer later.
    """
    raw = os.environ.get(PRIVATE_KEY_ENV, "").strip()
    if raw:
        return _seed_from_b64(raw, PRIVATE_KEY_ENV)
    path = resolve_key_path(key_path)
    if path.is_file():
        return _seed_from_b64(path.read_text(encoding="utf-8"), str(path))
    if os.environ.get(ALLOW_EPHEMERAL_KEY_ENV, "").strip() == "1":
        return Ed25519PrivateKey.generate()
    raise EphemeralKeyRefused(
        f"no signing key: {PRIVATE_KEY_ENV} is unset and {path} does not exist. "
        "A receipt you keep needs a persistent key — create one with "
        "`orcha record keygen`, or set ATTESTATION_ALLOW_EPHEMERAL_KEY=1 "
        "(dev/test only: the receipt will not verify against a key anyone holds)."
    )


def write_key_file(
    key_path: str | os.PathLike[str] | None = None, *, force: bool = False
) -> tuple[Path, Ed25519PrivateKey]:
    """Generate a new Ed25519 seed and write it base64-encoded, mode ``0600``.

    Refuses to overwrite an existing file unless *force*. The seed is
    returned as a key object only; it is never rendered to stdout or a log.
    """
    path = resolve_key_path(key_path)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to replace it")
    private_key = Ed25519PrivateKey.generate()
    seed_b64 = base64.b64encode(private_key.private_bytes_raw()).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(seed_b64 + "\n")
    finally:
        del seed_b64
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path, private_key


# ── One-call seal ────────────────────────────────────────────────────────────


def seal_run(run: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Build and sign an envelope from a run description in one call.

    *run* carries ``run_id``, ``agent_dids``, ``policy_version``, ``steps``
    (raw), ``verdicts``, ``started_at``, ``finished_at``, and optionally
    ``charter_hash`` (default ``None``) and ``signer_did`` (default
    :func:`key_did` of the key). The signer's public key is always derived
    from *private_key*; a ``signer`` object in *run* is rejected so an
    envelope can never name a key it was not signed with.
    """
    _require(isinstance(run, dict), "run must be a JSON object")
    _require("signer" not in run, "run.signer is derived from the key; remove it")
    required = {
        "run_id",
        "agent_dids",
        "policy_version",
        "steps",
        "verdicts",
        "started_at",
        "finished_at",
    }
    missing = required - set(run)
    _require(not missing, f"run is missing {sorted(missing)}")
    allowed = required | {"charter_hash", "signer_did"}
    unknown = set(run) - allowed
    _require(not unknown, f"run has unknown fields {sorted(unknown)}")
    signer_did = run.get("signer_did")
    if signer_did is None:
        signer_did = key_did(private_key)
    unsigned = build_run_envelope(
        run_id=run["run_id"],
        agent_dids=run["agent_dids"],
        charter_hash=run.get("charter_hash"),
        policy_version=run["policy_version"],
        steps=run["steps"],
        verdicts=run["verdicts"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        signer_did=signer_did,
        public_key_b64=public_key_b64_of(private_key),
    )
    return sign_run_envelope(unsigned, private_key)
