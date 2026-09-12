"""RFC 0003 tests: run attestation envelope build / sign / verify / observer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import ExplodingDB, FakeDB, _step_result
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from emerge_node.envelope import canonical_json_bytes
from validator import signer
from validator.run_envelope import (
    build_run_envelope,
    cdv_score_to_bp,
    compute_envelope_digest,
    compute_steps_merkle_root,
    compute_steps_root,
    get_latest_run_attestation_for_session,
    get_run_attestation_by_run_id,
    get_run_attestation_record,
    persist_run_attestation,
    sign_run_envelope,
    verify_run_envelope,
)
from validator.run_observer import RunAttestationObserver

# RFC 0003 worked example — example-only Ed25519 seed 00 01 02 … 1f.
EXAMPLE_SEED_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
EXAMPLE_PUBLIC_KEY_B64 = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
EXAMPLE_H0 = "181c12df1f221316f3353e5a153d2cd2a79672b30f3830f83ad325dbaade707d"
EXAMPLE_STEPS_ROOT = "8de66de79f1eeab8c984588fd82aaab604a018ead0f04af43102ea2baab2557e"
EXAMPLE_STEPS_MERKLE_ROOT = (
    "96267180943447e517b2698c400a785f69bb67bff99abd1c3f9738512f9e4362"
)
EXAMPLE_DIGEST = "7f7ca86fdbc4ffd3cae7acb03f7f8f93b580d0a76c8a601a703f6ce56c79d8ef"
EXAMPLE_SIGNATURE = (
    "Xwbefu8AXyI9wXgHapNu/GXGhinwF9jWlfnoi8qsevpz"
    "i3T0E+pHmymBEqO4/iJ9hMBKXEb7NTHQyEPirxDgDw=="
)

EXAMPLE_STEPS_RAW = [
    {
        "call_id": "call-7f3a",
        "tool": "search_docs",
        "args": {"query": "refund policy"},
        "output": {"documents": ["policy-v3"], "count": 1},
        "success": True,
        "latency_ms": 132,
    },
    {
        "call_id": "call-7f3b",
        "tool": "summarize",
        "args": {"text": "policy-v3 contents", "max_words": 50},
        "output": {"summary": "Refunds within 30 days."},
        "success": True,
        "latency_ms": 481,
        "cdv_bp": 910,
    },
]

EXAMPLE_ENVELOPE = {
    "format": "orcha.run-attestation/v1",
    "run_id": "run-9c2e-example",
    "agent_dids": ["did:orcha:agent:example-support-bot"],
    "charter_hash": None,
    "policy_version": "example-policy/1.0",
    "steps": [
        {
            "seq": 0,
            "call_id": "call-7f3a",
            "tool": "search_docs",
            "args_hash": "cfb7b9e24993e2079be817f26458fbfe854c97ecca332a83bf03bc33912064a0",
            "output_hash": "b53a450afe33a989f27439fb2832a7258f4dd4ed2b883c82d34571d9c8b713e6",
            "success": True,
            "latency_ms": 132,
        },
        {
            "seq": 1,
            "call_id": "call-7f3b",
            "tool": "summarize",
            "args_hash": "49d58a9a2fde08cd152b791aaa7ab455d90019dffcc646c7fef87c9e25762134",
            "output_hash": "32cd234e055db6061fb557f3d4dafdbccad80c904b9342b52b53f8870d0a80bd",
            "success": True,
            "latency_ms": 481,
            "cdv_bp": 910,
        },
    ],
    "steps_root": EXAMPLE_STEPS_ROOT,
    "steps_merkle_root": EXAMPLE_STEPS_MERKLE_ROOT,
    "verdicts": [{"check": "authorized_scope", "result": "pass"}],
    "started_at": "2026-08-06T00:58:21Z",
    "finished_at": "2026-08-06T00:58:24Z",
    "signer": {
        "did": "did:orcha:system:validator",
        "public_key_b64": EXAMPLE_PUBLIC_KEY_B64,
    },
    "signature": EXAMPLE_SIGNATURE,
}


def _example_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(EXAMPLE_SEED_B64))


def _build_example_unsigned() -> dict[str, Any]:
    return build_run_envelope(
        run_id="run-9c2e-example",
        agent_dids=["did:orcha:agent:example-support-bot"],
        charter_hash=None,
        policy_version="example-policy/1.0",
        steps=EXAMPLE_STEPS_RAW,
        verdicts=[{"check": "authorized_scope", "result": "pass"}],
        started_at="2026-08-06T00:58:21Z",
        finished_at="2026-08-06T00:58:24Z",
        signer_did="did:orcha:system:validator",
        public_key_b64=EXAMPLE_PUBLIC_KEY_B64,
    )


# ── Golden vector: the RFC 0003 worked example ──────────────────────────────


def test_golden_rfc_worked_example() -> None:
    unsigned = _build_example_unsigned()

    # Exact hashed step entries from the RFC table.
    assert unsigned["steps"] == EXAMPLE_ENVELOPE["steps"]
    # Chain head for step 0 and the chain root.
    assert (
        hashlib.sha256(canonical_json_bytes(unsigned["steps"][0])).hexdigest()
        == EXAMPLE_H0
    )
    assert unsigned["steps_root"] == EXAMPLE_STEPS_ROOT
    assert compute_steps_root(unsigned["steps"]) == EXAMPLE_STEPS_ROOT
    # ...and the Merkle Tree Hash over the same canonical step bytes.
    assert unsigned["steps_merkle_root"] == EXAMPLE_STEPS_MERKLE_ROOT
    assert compute_steps_merkle_root(unsigned["steps"]) == EXAMPLE_STEPS_MERKLE_ROOT
    # Two independent commitments — equal roots would mean one is derived.
    assert EXAMPLE_STEPS_ROOT != EXAMPLE_STEPS_MERKLE_ROOT
    # The signed digest quoted in the RFC prose.
    assert compute_envelope_digest(unsigned) == EXAMPLE_DIGEST

    signed = sign_run_envelope(unsigned, _example_private_key())
    # Byte-exact match with the full envelope published in the RFC.
    assert signed == EXAMPLE_ENVELOPE
    assert verify_run_envelope(signed) is True


def test_golden_cdv_bp_is_an_integer_in_the_canonical_bytes() -> None:
    """cdv_bp renders as plain decimal digits — no float anywhere in a step."""
    unsigned = _build_example_unsigned()
    canonical = canonical_json_bytes(unsigned["steps"][1])
    assert b'"cdv_bp":910' in canonical
    assert b"." not in canonical.split(b'"cdv_bp":')[1].split(b",")[0]
    assert "cdv_score" not in unsigned["steps"][1]


def test_cdv_score_to_bp_rounds_and_clamps() -> None:
    assert cdv_score_to_bp(0.91) == 910
    assert cdv_score_to_bp(1.0) == 1000
    assert cdv_score_to_bp(0.0) == 0
    assert cdv_score_to_bp(0.12345) == 123
    assert cdv_score_to_bp(1.7) == 1000
    assert cdv_score_to_bp(-0.2) == 0
    assert isinstance(cdv_score_to_bp(0.5), int)


def test_charset_rule_rejected_at_build_and_at_verify() -> None:
    """Free-text fields are printable ASCII: the builder refuses, the verifier
    fails schema, and a conformant envelope still verifies after the check."""
    base = {
        "run_id": "run-charset",
        "agent_dids": ["did:orcha:agent:example-support-bot"],
        "charter_hash": None,
        "policy_version": "p/1",
        "steps": [
            {
                "call_id": "c",
                "tool": "t",
                "args": {},
                "output": None,
                "success": True,
                "latency_ms": 1,
            }
        ],
        "verdicts": [],
        "started_at": "2026-08-06T01:00:00Z",
        "finished_at": "2026-08-06T01:00:00Z",
        "signer_did": "did:orcha:system:validator",
        "public_key_b64": EXAMPLE_PUBLIC_KEY_B64,
    }
    with pytest.raises(ValueError, match="run_id"):
        build_run_envelope(**{**base, "run_id": "run-\u00e9"})
    with pytest.raises(ValueError, match="tool"):
        build_run_envelope(
            **{**base, "steps": [{**base["steps"][0], "tool": "r\u00e9sum\u00e9"}]}
        )
    with pytest.raises(ValueError, match="call_id"):
        build_run_envelope(
            **{**base, "steps": [{**base["steps"][0], "call_id": "tab\there"}]}
        )
    with pytest.raises(ValueError, match="agent_dids"):
        build_run_envelope(**{**base, "agent_dids": ["did:orcha:agent:\u00fc"]})
    with pytest.raises(ValueError, match=r"verdicts\[\]\.check"):
        build_run_envelope(
            **{**base, "verdicts": [{"check": "\u00e7", "result": "pass"}]}
        )
    with pytest.raises(ValueError, match="detail"):
        build_run_envelope(
            **{
                **base,
                "verdicts": [{"check": "c", "result": "pass", "detail": "\x7f"}],
            }
        )
    # Empty detail is allowed by the rule; non-empty ASCII is too.
    ok = build_run_envelope(
        **{**base, "verdicts": [{"check": "c", "result": "pass", "detail": ""}]}
    )
    assert ok["verdicts"] == [{"check": "c", "result": "pass", "detail": ""}]

    signed = sign_run_envelope(
        build_run_envelope(**base), private_key=_example_private_key()
    )
    assert verify_run_envelope(signed) is True
    for field, value in (
        ("run_id", "run-\u00e9"),
        ("policy_version", "p/\u00e9"),
    ):
        assert verify_run_envelope({**signed, field: value}) is False
    bad_step = {**signed, "steps": [{**signed["steps"][0], "tool": "\u00e9"}]}
    assert verify_run_envelope(bad_step) is False
    bad_bp = {**signed, "steps": [{**signed["steps"][0], "cdv_bp": 1001}]}
    assert verify_run_envelope(bad_bp) is False
    float_bp = {**signed, "steps": [{**signed["steps"][0], "cdv_bp": 0.91}]}
    assert verify_run_envelope(float_bp) is False
    old_field = {**signed, "steps": [{**signed["steps"][0], "cdv_score": 0.91}]}
    assert verify_run_envelope(old_field) is False


def test_shared_golden_fixture() -> None:
    """The shared fixture (consumed by sdk/node tests too) matches the RFC
    worked example byte-for-byte and verifies; the tampered variant fails."""
    golden_path = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "spec"
        / "test-vectors"
        / "run-attestation-golden.json"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert golden["valid"] == EXAMPLE_ENVELOPE
    assert verify_run_envelope(golden["valid"]) is True
    assert verify_run_envelope(golden["tampered"]) is False


# ── Roundtrip / tamper / chain-break ────────────────────────────────────────


def _roundtrip_envelope() -> dict[str, Any]:
    unsigned = build_run_envelope(
        run_id="run-roundtrip",
        agent_dids=["did:orcha:agent:roundtrip-bot"],
        charter_hash=hashlib.sha256(b"charter").hexdigest(),
        policy_version="policy/2.1",
        steps=[
            {
                "call_id": "c1",
                "tool": "search",
                "args": {"q": "x"},
                "output": {"hits": []},
                "success": True,
                "latency_ms": 12,
            },
            {
                "call_id": "c2",
                "tool": "fetch",
                "args": {"url": "https://example.com"},
                "output": "page text",
                "success": False,
                "latency_ms": 340,
            },
        ],
        verdicts=[{"check": "authorized_scope", "result": "warn", "detail": "d"}],
        started_at="2026-08-06T01:00:00Z",
        finished_at="2026-08-06T01:00:02Z",
        signer_did="did:orcha:system:validator",
        public_key_b64=EXAMPLE_PUBLIC_KEY_B64,
    )
    return sign_run_envelope(unsigned, _example_private_key())


def test_roundtrip_build_sign_verify() -> None:
    signed = _roundtrip_envelope()
    assert verify_run_envelope(signed) is True
    # Explicit public key override verifies the same envelope.
    assert verify_run_envelope(signed, EXAMPLE_PUBLIC_KEY_B64) is True
    # A different key must fail.
    other = Ed25519PrivateKey.from_private_bytes(b"\x09" * 32)
    other_pub = base64.b64encode(
        other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    assert verify_run_envelope(signed, other_pub) is False


def test_tampered_step_hashes_fail_verification() -> None:
    signed = _roundtrip_envelope()
    for field in ("args_hash", "output_hash"):
        tampered = json.loads(json.dumps(signed))
        tampered["steps"][0][field] = hashlib.sha256(b"tampered").hexdigest()
        assert verify_run_envelope(tampered) is False


def test_reordered_steps_fail_verification() -> None:
    signed = _roundtrip_envelope()
    tampered = json.loads(json.dumps(signed))
    tampered["steps"] = list(reversed(tampered["steps"]))
    assert verify_run_envelope(tampered) is False


def test_chain_break_on_altered_step() -> None:
    signed = _roundtrip_envelope()
    altered = json.loads(json.dumps(signed["steps"]))
    altered[1]["latency_ms"] = 341
    assert compute_steps_root(altered) != signed["steps_root"]
    assert compute_steps_merkle_root(altered) != signed["steps_merkle_root"]


def test_empty_run_chain_root_is_empty_hash() -> None:
    assert compute_steps_root([]) == hashlib.sha256(b"").hexdigest()
    assert compute_steps_merkle_root([]) == hashlib.sha256(b"").hexdigest()
    unsigned = build_run_envelope(
        run_id="run-empty",
        agent_dids=["did:orcha:agent:noop-bot"],
        charter_hash=None,
        policy_version="policy/1.0",
        steps=[],
        verdicts=[],
        started_at="2026-08-06T01:00:00Z",
        finished_at="2026-08-06T01:00:00Z",
        signer_did="did:orcha:system:validator",
        public_key_b64=EXAMPLE_PUBLIC_KEY_B64,
    )
    assert unsigned["steps_root"] == hashlib.sha256(b"").hexdigest()
    assert unsigned["steps_merkle_root"] == hashlib.sha256(b"").hexdigest()
    signed = sign_run_envelope(unsigned, _example_private_key())
    assert verify_run_envelope(signed) is True


def test_schema_check_rejects_bad_envelopes() -> None:
    signed = _roundtrip_envelope()

    unknown_field = {**signed, "extra": "nope"}
    assert verify_run_envelope(unknown_field) is False

    bad_format = {**signed, "format": "orcha.run-attestation/v2"}
    assert verify_run_envelope(bad_format) is False

    bad_did = json.loads(json.dumps(signed))
    bad_did["agent_dids"] = ["not-a-did"]  # method restriction is profile, not schema
    assert verify_run_envelope(bad_did) is False

    gapped = json.loads(json.dumps(signed))
    gapped["steps"][1]["seq"] = 7
    assert verify_run_envelope(gapped) is False

    bad_hex = json.loads(json.dumps(signed))
    bad_hex["steps_root"] = "zz" + bad_hex["steps_root"][2:]
    assert verify_run_envelope(bad_hex) is False

    bad_merkle_hex = json.loads(json.dumps(signed))
    bad_merkle_hex["steps_merkle_root"] = "zz" + bad_merkle_hex["steps_merkle_root"][2:]
    assert verify_run_envelope(bad_merkle_hex) is False

    missing_merkle = {k: v for k, v in signed.items() if k != "steps_merkle_root"}
    assert verify_run_envelope(missing_merkle) is False


def test_builder_rejects_invalid_inputs() -> None:
    base = {
        "run_id": "run-x",
        "agent_dids": ["did:orcha:agent:x"],
        "charter_hash": None,
        "policy_version": "p/1",
        "steps": [],
        "verdicts": [],
        "started_at": "2026-08-06T01:00:00Z",
        "finished_at": "2026-08-06T01:00:00Z",
        "signer_did": "did:orcha:system:validator",
        "public_key_b64": EXAMPLE_PUBLIC_KEY_B64,
    }
    with pytest.raises(ValueError, match="agent_dids"):
        build_run_envelope(**{**base, "agent_dids": ["not-a-did"]})
    with pytest.raises(ValueError, match="signer_did"):
        build_run_envelope(**{**base, "signer_did": "did:emerge:system:x"})
    with pytest.raises(ValueError, match="charter_hash"):
        build_run_envelope(**{**base, "charter_hash": "abc"})
    with pytest.raises(ValueError, match="cdv_bp"):
        build_run_envelope(
            **{
                **base,
                "steps": [
                    {
                        "call_id": "c",
                        "tool": "t",
                        "args": {},
                        "output": None,
                        "success": True,
                        "latency_ms": 1,
                        "cdv_bp": 1500,
                    }
                ],
            }
        )


# ── Key handling (ATTESTATION_PRIVATE_KEY_B64 / ephemeral fallback) ─────────


def test_ephemeral_key_mode_warns_and_verifies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unsigned = _build_example_unsigned()
    # Drop the explicit key so the builder/signer fall back to get_signing_key().
    unsigned.pop("signer")
    with caplog.at_level(logging.WARNING, logger="validator.signer"):
        unsigned = build_run_envelope(
            run_id="run-eph",
            agent_dids=["did:orcha:agent:eph-bot"],
            charter_hash=None,
            policy_version="p/1",
            steps=EXAMPLE_STEPS_RAW,
            verdicts=[],
            started_at="2026-08-06T01:00:00Z",
            finished_at="2026-08-06T01:00:01Z",
            signer_did="did:orcha:system:validator",
        )
        signed = sign_run_envelope(unsigned)

    assert any("EPHEMERAL" in record.message for record in caplog.records)
    assert signed["signer"]["public_key_b64"]
    assert verify_run_envelope(signed) is True


def test_env_key_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(signer.PRIVATE_KEY_ENV, EXAMPLE_SEED_B64)
    signer._reset_signing_key_for_tests()

    unsigned = build_run_envelope(
        run_id="run-env",
        agent_dids=["did:orcha:agent:env-bot"],
        charter_hash=None,
        policy_version="p/1",
        steps=[],
        verdicts=[],
        started_at="2026-08-06T01:00:00Z",
        finished_at="2026-08-06T01:00:01Z",
        signer_did="did:orcha:system:validator",
    )
    assert unsigned["signer"]["public_key_b64"] == EXAMPLE_PUBLIC_KEY_B64
    signed = sign_run_envelope(unsigned)
    assert verify_run_envelope(signed) is True


# ── RunAttestationObserver ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observer_builds_signs_and_persists_envelope() -> None:
    db = FakeDB()
    observer = RunAttestationObserver(db=db)
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_step_complete(
        _step_result(
            "c2",
            tool_name="summarize",
            content="summary",
            latency_ms=200,
            completed_at="2026-08-06T01:00:03+00:00",
            metadata={"cdv": {"score": 0.91, "passed": True}},
        )
    )
    await observer.on_run_complete("sess-1")

    assert len(observer.envelopes) == 1
    envelope = next(iter(observer.envelopes.values()))
    assert verify_run_envelope(envelope) is True
    assert envelope["format"] == "orcha.run-attestation/v1"
    assert envelope["agent_dids"] == ["did:orcha:agent:rulebook-rag"]
    assert [s["seq"] for s in envelope["steps"]] == [0, 1]
    assert [s["call_id"] for s in envelope["steps"]] == ["c1", "c2"]
    assert envelope["steps"][1]["cdv_bp"] == 910
    assert "cdv_bp" not in envelope["steps"][0]
    # Raw args/content are never embedded — hashes only.
    canonical = canonical_json_bytes(envelope).decode("utf-8")
    assert "refund policy" not in canonical
    assert "summary" not in canonical
    assert envelope["started_at"].endswith("Z")
    assert envelope["finished_at"] == "2026-08-06T01:00:03Z"
    assert envelope["verdicts"] == [
        {"check": "structural_verification", "result": "pass", "detail": "c1: ok"},
        {"check": "structural_verification", "result": "pass", "detail": "c2: ok"},
    ]

    # Persisted via the Attestation model (status pending).
    assert len(db.attestation.rows) == 1
    row = next(iter(db.attestation.rows.values()))
    assert row.session_id == "sess-1"
    assert row.case_hash == compute_envelope_digest(envelope)
    assert row.signature == envelope["signature"]
    assert row.public_key == envelope["signer"]["public_key_b64"]
    assert row.status == "pending"
    # _prisma_json wraps the payload in the real Prisma client; raw dict on fakes.
    payload = getattr(row.payload, "data", row.payload)
    assert payload["format"] == "orcha.run-attestation/v1"


@pytest.mark.asyncio
async def test_observer_run_without_steps_emits_nothing() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_run_complete("sess-empty")
    assert observer.envelopes == {}
    assert observer.last_sealed == {}
    assert observer.published == {}


@pytest.mark.asyncio
async def test_observer_records_last_sealed_binding() -> None:
    """Seal records the session→run_id handoff the settlement gate pops (2.1)."""
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_run_complete("sess-1")

    assert list(observer.last_sealed) == ["sess-1"]
    sealed_run_id = observer.last_sealed["sess-1"]
    assert observer.published["sess-1"] == sealed_run_id
    assert sealed_run_id in observer.envelopes
    assert observer.envelopes[sealed_run_id]["run_id"] == sealed_run_id

    # A second run on the same session overwrites the binding with its own.
    await observer.on_step_complete(_step_result("c2"))
    await observer.on_run_complete("sess-1")
    assert observer.last_sealed["sess-1"] != sealed_run_id
    assert observer.published["sess-1"] == observer.last_sealed["sess-1"]
    observer.last_sealed.pop("sess-1")
    assert observer.published["sess-1"] != sealed_run_id


@pytest.mark.asyncio
async def test_observer_db_outage_keeps_envelope_in_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observer = RunAttestationObserver(db=ExplodingDB())
    await observer.on_step_complete(_step_result("c1"))
    with caplog.at_level(logging.WARNING, logger="validator.run_envelope"):
        await observer.on_run_complete("sess-1")  # must not raise

    assert len(observer.envelopes) == 1
    envelope = next(iter(observer.envelopes.values()))
    assert verify_run_envelope(envelope) is True


@pytest.mark.asyncio
async def test_observer_never_raises_on_bad_records() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    # Non-DID agent id → envelope build fails closed; the run is unaffected.
    await observer.on_step_complete(_step_result("c1", agent_id="not-a-did"))
    await observer.on_run_complete("sess-1")
    assert observer.envelopes == {}


# ── RunAttestationObserver: abandonment, retention bound, persist timeout ────


@pytest.mark.asyncio
async def test_observer_discard_run_drops_buffered_steps() -> None:
    """Error/cancel path: discard_run clears the buffer, nothing is sealed."""
    db = FakeDB()
    observer = RunAttestationObserver(db=db)
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_step_complete(_step_result("c2"))

    await observer.discard_run("sess-1")

    assert observer._steps == {}
    # A later run-complete dispatch for the abandoned run seals nothing.
    await observer.on_run_complete("sess-1")
    assert observer.envelopes == {}
    assert db.attestation.rows == {}


@pytest.mark.asyncio
async def test_observer_discard_prevents_cross_turn_contamination() -> None:
    """Steps from an abandoned run must not leak into the next turn's envelope."""
    observer = RunAttestationObserver(db=FakeDB())
    # Turn 1: two steps, then the run errors out before its boundary.
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_step_complete(_step_result("c2"))
    await observer.discard_run("sess-1")
    # Turn 2: a fresh run for the same session.
    await observer.on_step_complete(_step_result("c3"))
    await observer.on_run_complete("sess-1")

    assert len(observer.envelopes) == 1
    envelope = next(iter(observer.envelopes.values()))
    assert [s["call_id"] for s in envelope["steps"]] == ["c3"]


@pytest.mark.asyncio
async def test_observer_discard_unknown_session_is_noop() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    await observer.discard_run("sess-never-seen")  # must not raise


@pytest.mark.asyncio
async def test_observer_envelope_retention_is_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The in-memory fallback map evicts the oldest envelope past the bound."""
    observer = RunAttestationObserver(db=FakeDB(), max_envelopes=2)
    with caplog.at_level(logging.WARNING, logger="validator.run_observer"):
        for i in range(3):
            await observer.on_step_complete(
                _step_result(f"c{i}", session_id=f"sess-{i}")
            )
            await observer.on_run_complete(f"sess-{i}")

    assert len(observer.envelopes) == 2
    # Insertion order: sess-0's envelope was evicted, sess-1/sess-2 retained.
    retained = sorted(k.rsplit("-", 1)[0] for k in observer.envelopes)
    assert retained == ["sess-1", "sess-2"]
    assert any("evicted oldest envelope" in r.message for r in caplog.records)


class HangingAttestationTable:
    """DB stand-in whose create() never returns (hung connection simulation)."""

    async def create(self, data: dict[str, Any]) -> Any:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class HangingDB:
    def __init__(self) -> None:
        self.attestation = HangingAttestationTable()


@pytest.mark.asyncio
async def test_observer_persist_timeout_keeps_envelope_in_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung DB must not stall run completion; the envelope stays in memory."""
    observer = RunAttestationObserver(db=HangingDB(), persist_timeout=0.05)
    await observer.on_step_complete(_step_result("c1"))

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="validator.run_observer"):
        await observer.on_run_complete("sess-1")  # must not raise or hang
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # bounded by persist_timeout, not the hung create()
    assert len(observer.envelopes) == 1
    envelope = next(iter(observer.envelopes.values()))
    assert verify_run_envelope(envelope) is True
    assert any("persist timed out" in r.message for r in caplog.records)


def test_superagent_step_result_carries_args() -> None:
    """The observer contract needs raw args on StepResult for args_hash."""
    from superagent.middleware.observers import StepResult

    record = StepResult(
        call_id="c1",
        agent_id="did:orcha:agent:x",
        capability_id="cap",
        protocol="MCP",
        tool_name="tool",
        success=True,
        content="out",
        args={"q": "x"},
    )
    assert record.args == {"q": "x"}
    # Default keeps older constructors working.
    assert (
        StepResult(
            call_id="c2",
            agent_id="a",
            capability_id="cap",
            protocol="MCP",
            tool_name="tool",
            success=True,
            content="out",
        ).args
        == {}
    )


# ── run_id persistence + gate lookup (Story 1.1, AD-4/AD-10) ─────────────────


def _signed_envelope(run_id: str) -> dict[str, Any]:
    unsigned = build_run_envelope(
        run_id=run_id,
        agent_dids=["did:orcha:agent:rulebook-rag"],
        charter_hash=None,
        policy_version="p/1",
        steps=[dict(EXAMPLE_STEPS_RAW[0])],
        verdicts=[],
        started_at="2026-08-06T01:00:00Z",
        finished_at="2026-08-06T01:00:01Z",
        signer_did="did:orcha:system:validator",
    )
    return sign_run_envelope(unsigned)


@pytest.mark.asyncio
async def test_persist_run_attestation_stores_run_id() -> None:
    db = FakeDB()
    envelope = _signed_envelope("sess-1-abc123def456")
    result = await persist_run_attestation("sess-1", envelope, db=db)

    assert result is not None
    row = next(iter(db.attestation.rows.values()))
    assert row.run_id == envelope["run_id"]


@pytest.mark.asyncio
async def test_get_run_attestation_by_run_id_returns_envelope() -> None:
    db = FakeDB()
    envelope = _signed_envelope("sess-1-abc123def456")
    await persist_run_attestation("sess-1", envelope, db=db)

    found = await get_run_attestation_by_run_id("sess-1-abc123def456", db=db)

    assert found is not None
    assert found["run_id"] == envelope["run_id"]
    assert found["format"] == "orcha.run-attestation/v1"
    assert verify_run_envelope(found) is True


@pytest.mark.asyncio
async def test_get_run_attestation_by_run_id_unknown_returns_none() -> None:
    db = FakeDB()
    assert await get_run_attestation_by_run_id("never-seen", db=db) is None


@pytest.mark.asyncio
async def test_get_run_attestation_by_run_id_db_down_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="validator.run_envelope"):
        found = await get_run_attestation_by_run_id("x", db=ExplodingDB())

    assert found is None
    assert "unavailable" in caplog.text


# ── Code-review hardening (Story 1.1 review follow-ups) ─────────────────────


@pytest.mark.asyncio
async def test_get_run_attestation_by_run_id_rejects_bad_input() -> None:
    """None/empty/non-string run_id must not query (None would match NULL rows)."""
    db = FakeDB()
    assert await get_run_attestation_by_run_id(None, db=db) is None
    assert await get_run_attestation_by_run_id("", db=db) is None
    assert await get_run_attestation_by_run_id(123, db=db) is None


@pytest.mark.asyncio
async def test_get_run_attestation_by_run_id_corrupt_payload_returns_none() -> None:
    """A corrupt (non-dict) stored payload collapses to None, never to caller."""
    db = FakeDB()
    db.attestation.rows["att-9"] = SimpleNamespace(
        id="att-9", run_id="sess-corrupt", payload="not-a-dict"
    )
    assert await get_run_attestation_by_run_id("sess-corrupt", db=db) is None
    assert await get_run_attestation_record("sess-corrupt", db=db) is None


@pytest.mark.asyncio
async def test_persist_run_attestation_summary_includes_run_id() -> None:
    db = FakeDB()
    envelope = _signed_envelope("sess-1-summary01")
    result = await persist_run_attestation("sess-1", envelope, db=db)
    assert result is not None
    assert result["run_id"] == envelope["run_id"]


def test_schema_accepts_any_did_method_but_the_producer_enforces_the_profile() -> None:
    """RFC 0003: the schema binds DID syntax; the did:orcha restriction is the
    platform profile, enforced by this producer and by the settlement gate."""
    signed = _roundtrip_envelope()
    unsigned = {k: v for k, v in signed.items() if k != "signature"}
    for did in (
        "did:web:example.com",
        "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
    ):
        external = json.loads(json.dumps(unsigned))
        external["agent_dids"] = [did]
        signed_external = sign_run_envelope(external, _example_private_key())
        assert verify_run_envelope(signed_external) is True
    for did in (
        "did:",
        "did:web",
        "did:Web:x",
        "web:x",
        "did:web:\u00fc",
        "did:web:x\x7f",
    ):
        malformed = json.loads(json.dumps(signed))
        malformed["agent_dids"] = [did]
        assert verify_run_envelope(malformed) is False
    base = {
        "run_id": "run-x",
        "agent_dids": ["did:orcha:agent:x"],
        "charter_hash": None,
        "policy_version": "p/1",
        "steps": [],
        "verdicts": [],
        "started_at": "2026-08-06T01:00:00Z",
        "finished_at": "2026-08-06T01:00:00Z",
        "signer_did": "did:orcha:system:validator",
        "public_key_b64": EXAMPLE_PUBLIC_KEY_B64,
    }
    with pytest.raises(ValueError, match="agent_dids"):
        build_run_envelope(**{**base, "agent_dids": ["did:web:example.com"]})
    with pytest.raises(ValueError, match="signer_did"):
        build_run_envelope(**{**base, "signer_did": "did:key:z6Mk"})


@pytest.mark.asyncio
async def test_get_run_attestation_record_includes_session_id() -> None:
    db = FakeDB()
    envelope = _signed_envelope("sess-1-rec01")
    await persist_run_attestation("sess-1", envelope, db=db)

    record = await get_run_attestation_record("sess-1-rec01", db=db)

    assert record is not None
    assert record["session_id"] == "sess-1"
    assert record["run_id"] == "sess-1-rec01"
    assert record["envelope"]["run_id"] == envelope["run_id"]


@pytest.mark.asyncio
async def test_get_latest_run_attestation_for_session_returns_newest() -> None:
    db = FakeDB()
    first = _signed_envelope("sess-1-old01")
    second = _signed_envelope("sess-1-new01")
    await persist_run_attestation("sess-1", first, db=db)
    await persist_run_attestation("sess-1", second, db=db)

    record = await get_latest_run_attestation_for_session("sess-1", db=db)

    assert record is not None
    assert record["run_id"] == "sess-1-new01"


@pytest.mark.asyncio
async def test_get_latest_run_attestation_for_session_unknown_returns_none() -> None:
    assert await get_latest_run_attestation_for_session("never", db=FakeDB()) is None


@pytest.mark.asyncio
async def test_get_latest_run_attestation_rejects_bad_input() -> None:
    db = FakeDB()
    assert await get_latest_run_attestation_for_session(None, db=db) is None
    assert await get_latest_run_attestation_for_session("", db=db) is None
    assert await get_run_attestation_record(None, db=db) is None
