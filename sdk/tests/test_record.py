"""Tests for the local RFC 0003 producer (`emerge.record`, `orcha record`).

The pin is the shared golden vector: built from the RFC worked example's raw
inputs and the example-only seed, the SDK producer must reproduce
``run-attestation-golden.json`` ``valid`` byte-for-byte (including field
order) and ``tampered`` after the same one-byte change. That is the same
acceptance the platform producer meets, so the two cannot drift apart
silently.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge.cli import main
from emerge.record import (
    ALLOW_EPHEMERAL_KEY_ENV,
    KEY_PATH_ENV,
    PRIVATE_KEY_ENV,
    EphemeralKeyRefused,
    build_run_envelope,
    key_did,
    load_signing_key,
    public_key_b64_of,
    seal_run,
    sign_run_envelope,
    write_key_file,
)
from emerge.run_attestation import compute_envelope_digest, verify_run_attestation

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_DIR = REPO_ROOT / "docs" / "spec" / "test-vectors"
GOLDEN_PATH = VECTORS_DIR / "run-attestation-golden.json"
GENERATOR_PATH = VECTORS_DIR / "generate_run_attestation_golden.py"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location(
        "generate_run_attestation_golden", GENERATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def example_key(generator) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(generator.EXAMPLE_SEED)


@pytest.fixture
def example_run(generator) -> dict:
    """The RFC worked example as a `record seal` run description."""
    return {
        "run_id": "run-9c2e-example",
        "agent_dids": ["did:orcha:agent:example-support-bot"],
        "charter_hash": None,
        "policy_version": "example-policy/1.0",
        "steps": copy.deepcopy(generator.EXAMPLE_RAW_STEPS),
        "verdicts": [{"check": "authorized_scope", "result": "pass"}],
        "started_at": "2026-08-06T00:58:21Z",
        "finished_at": "2026-08-06T00:58:24Z",
        "signer_did": "did:orcha:system:validator",
    }


@pytest.fixture
def no_ambient_key(monkeypatch, tmp_path):
    """No env seed, no ephemeral opt-in, key path pointed at an absent file."""
    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)
    monkeypatch.setenv(KEY_PATH_ENV, str(tmp_path / "absent" / "key"))
    return tmp_path


def _render(value: dict) -> str:
    return json.dumps(value, indent=2) + "\n"


# ── Golden pin: byte-for-byte against the shared vector ──────────────────────


def test_producer_reproduces_the_valid_vector_byte_for_byte(
    golden, example_run, example_key
):
    sealed = seal_run(example_run, example_key)
    assert sealed == golden["valid"]
    # Field order too — the committed file is the human-readable rendering.
    assert _render(sealed) == _render(golden["valid"])
    assert verify_run_attestation(sealed).valid is True


def test_build_and_sign_match_the_generator_step_by_step(
    golden, example_run, example_key, generator
):
    unsigned = build_run_envelope(
        run_id=example_run["run_id"],
        agent_dids=example_run["agent_dids"],
        charter_hash=None,
        policy_version=example_run["policy_version"],
        steps=example_run["steps"],
        verdicts=example_run["verdicts"],
        started_at=example_run["started_at"],
        finished_at=example_run["finished_at"],
        signer_did=example_run["signer_did"],
        public_key_b64=generator.EXAMPLE_PUBLIC_KEY_B64,
    )
    assert public_key_b64_of(example_key) == generator.EXAMPLE_PUBLIC_KEY_B64
    assert unsigned["steps"] == golden["valid"]["steps"]
    assert unsigned["steps_root"] == golden["valid"]["steps_root"]
    assert unsigned["steps_merkle_root"] == golden["valid"]["steps_merkle_root"]
    assert compute_envelope_digest(unsigned) == compute_envelope_digest(golden["valid"])
    signed = sign_run_envelope(unsigned, example_key)
    assert signed["signature"] == golden["valid"]["signature"]
    assert "signature" not in unsigned  # not mutated


def test_producer_reproduces_the_tampered_vector(golden, example_run, example_key):
    """Same one-byte change as the generator makes; every commitment breaks."""
    sealed = seal_run(example_run, example_key)
    tampered = copy.deepcopy(sealed)
    tampered["steps"][0]["latency_ms"] = 133
    assert tampered == golden["tampered"]
    assert _render(tampered) == _render(golden["tampered"])
    verdict = verify_run_attestation(tampered)
    assert verdict.valid is False
    assert verdict.checks["schema"] is True
    assert verdict.checks["steps_root"] is False


def test_resealing_the_tampered_inputs_is_a_different_valid_envelope(
    golden, example_run, example_key
):
    """Tampering the *inputs* and resealing is legitimate — and visibly not
    the golden envelope, because the roots and signature move with it."""
    run = copy.deepcopy(example_run)
    run["steps"][0]["latency_ms"] = 133
    resealed = seal_run(run, example_key)
    assert verify_run_attestation(resealed).valid is True
    assert resealed["steps_root"] != golden["valid"]["steps_root"]
    assert resealed["signature"] != golden["valid"]["signature"]


# ── Input guards (the platform producer's rules) ─────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda r: r["steps"][0].__setitem__("call_id", "call-é"), "call_id"),
        (lambda r: r["steps"][0].__setitem__("args", ["not", "object"]), "args"),
        (lambda r: r["steps"][0].__setitem__("success", 1), "success"),
        (lambda r: r["steps"][0].__setitem__("latency_ms", -1), "latency_ms"),
        (lambda r: r["steps"][1].__setitem__("cdv_bp", 1001), "cdv_bp"),
        (lambda r: r["steps"][1].__setitem__("cdv_bp", 0.91), "cdv_bp"),
        (lambda r: r["steps"][0].__setitem__("extra", 1), "unknown"),
        (lambda r: r["steps"][0].pop("output"), "missing"),
        (lambda r: r["agent_dids"].append("did:web:example.com"), "agent_dids"),
        (lambda r: r.__setitem__("signer_did", "did:key:z6Mk"), "signer_did"),
        (lambda r: r.__setitem__("charter_hash", "abc"), "charter_hash"),
        (lambda r: r.__setitem__("started_at", "2026-08-06 00:58:21"), "started_at"),
        (lambda r: r.__setitem__("policy_version", ""), "policy_version"),
        (lambda r: r["verdicts"].append({"check": "x", "result": "maybe"}), "result"),
        (
            lambda r: r["verdicts"].append({"check": "x", "result": "pass", "z": 1}),
            "verdicts",
        ),
        (lambda r: r.__setitem__("signer", {"did": "x"}), "signer"),
        (lambda r: r.__setitem__("bogus", 1), "unknown"),
    ],
)
def test_seal_rejects_nonconformant_input(example_run, example_key, mutate, fragment):
    run = copy.deepcopy(example_run)
    mutate(run)
    with pytest.raises(ValueError, match=fragment):
        seal_run(run, example_key)


def test_seal_derives_the_signer_did_from_the_key_when_omitted(
    example_run, example_key
):
    run = copy.deepcopy(example_run)
    del run["signer_did"]
    sealed = seal_run(run, example_key)
    assert sealed["signer"]["did"] == key_did(example_key)
    assert sealed["signer"]["did"].startswith("did:orcha:agent:key-")
    assert verify_run_attestation(sealed).valid is True


def test_empty_run_seals_and_verifies(example_run, example_key):
    run = {**copy.deepcopy(example_run), "steps": [], "verdicts": []}
    sealed = seal_run(run, example_key)
    assert sealed["steps"] == []
    assert verify_run_attestation(sealed).valid is True


# ── Keys: env wins, then the file, then refusal ─────────────────────────────


def test_no_key_anywhere_is_refused(no_ambient_key):
    with pytest.raises(EphemeralKeyRefused, match="keygen"):
        load_signing_key()


def test_ephemeral_opt_in_is_honoured(no_ambient_key, monkeypatch):
    monkeypatch.setenv(ALLOW_EPHEMERAL_KEY_ENV, "1")
    key = load_signing_key()
    assert isinstance(key, Ed25519PrivateKey)


def test_env_seed_wins_over_the_key_file(no_ambient_key, monkeypatch, generator):
    path, file_key = write_key_file(no_ambient_key / "key")
    monkeypatch.setenv(PRIVATE_KEY_ENV, generator.EXAMPLE_SEED_B64)
    loaded = load_signing_key(path)
    assert public_key_b64_of(loaded) == generator.EXAMPLE_PUBLIC_KEY_B64
    assert public_key_b64_of(loaded) != public_key_b64_of(file_key)


def test_key_file_is_used_when_env_is_unset(no_ambient_key):
    path, file_key = write_key_file(no_ambient_key / "key")
    loaded = load_signing_key(path)
    assert public_key_b64_of(loaded) == public_key_b64_of(file_key)


def test_key_file_is_mode_0600_and_never_overwritten_silently(no_ambient_key):
    path, first = write_key_file(no_ambient_key / "k" / "key")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    seed = base64.b64decode(path.read_text(encoding="utf-8").strip())
    assert len(seed) == 32
    with pytest.raises(FileExistsError, match="--force"):
        write_key_file(path)
    _, second = write_key_file(path, force=True)
    assert public_key_b64_of(second) != public_key_b64_of(first)


@pytest.mark.parametrize("bad", ["not base64!", base64.b64encode(b"short").decode()])
def test_malformed_env_seed_is_a_clear_error(no_ambient_key, monkeypatch, bad):
    monkeypatch.setenv(PRIVATE_KEY_ENV, bad)
    with pytest.raises(ValueError, match=PRIVATE_KEY_ENV):
        load_signing_key()


# ── CLI: seal → verify round trip ────────────────────────────────────────────


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_cli_seal_then_verify_round_trip(no_ambient_key, example_run, capsys):
    tmp = no_ambient_key
    assert main(["record", "keygen", "--key", str(tmp / "key")]) == 0
    keygen_out = capsys.readouterr()
    seed_b64 = (tmp / "key").read_text(encoding="utf-8").strip()
    assert seed_b64 not in keygen_out.out + keygen_out.err  # never printed

    run = {**copy.deepcopy(example_run)}
    del run["signer_did"]
    run_path = _write(tmp, "run.json", run)
    out_path = tmp / "envelope.json"
    rc = main(
        [
            "record",
            "seal",
            str(run_path),
            "--key",
            str(tmp / "key"),
            "--out",
            str(out_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "sealed run_id=run-9c2e-example steps=2" in captured.err
    assert f"verify with: orcha verify {out_path}" in captured.err
    assert seed_b64 not in captured.out + captured.err

    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    assert verify_run_attestation(envelope).valid is True
    assert main(["verify", str(out_path)]) == 0
    assert "Verdict: VALID" in capsys.readouterr().out


def test_cli_seal_writes_stdout_by_default_and_reads_stdin(
    no_ambient_key, example_run, monkeypatch, capsys
):
    write_key_file(no_ambient_key / "key")
    monkeypatch.setenv(KEY_PATH_ENV, str(no_ambient_key / "key"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(example_run)))
    rc = main(["record", "seal", "-"])
    captured = capsys.readouterr()
    assert rc == 0
    envelope = json.loads(captured.out)
    assert verify_run_attestation(envelope).valid is True
    assert envelope["signer"]["did"] == "did:orcha:system:validator"


def test_cli_seal_with_the_example_seed_emits_the_golden_envelope(
    no_ambient_key, example_run, generator, golden, monkeypatch, capsys
):
    monkeypatch.setenv(PRIVATE_KEY_ENV, generator.EXAMPLE_SEED_B64)
    run_path = _write(no_ambient_key, "run.json", example_run)
    assert main(["record", "seal", str(run_path)]) == 0
    assert json.loads(capsys.readouterr().out) == golden["valid"]


def test_cli_seal_refuses_without_a_key(no_ambient_key, example_run, capsys):
    run_path = _write(no_ambient_key, "run.json", example_run)
    rc = main(["record", "seal", str(run_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no signing key" in err
    assert "orcha record keygen" in err


def test_cli_seal_rejects_a_bad_run(no_ambient_key, example_run, capsys):
    write_key_file(no_ambient_key / "key")
    run = copy.deepcopy(example_run)
    run["steps"][0]["tool"] = "résumé"
    run_path = _write(no_ambient_key, "run.json", run)
    rc = main(["record", "seal", str(run_path), "--key", str(no_ambient_key / "key")])
    assert rc == 2
    assert "steps[].tool" in capsys.readouterr().err


def test_cli_seal_input_errors(no_ambient_key, monkeypatch, capsys):
    assert main(["record", "seal", "/nonexistent/run.json"]) == 2
    assert "cannot read" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert main(["record", "seal", "-"]) == 2
    assert "json" in capsys.readouterr().err.lower()
    monkeypatch.setattr("sys.stdin", io.StringIO("[1]"))
    assert main(["record", "seal", "-"]) == 2
    assert "JSON object" in capsys.readouterr().err


def test_cli_keygen_refuses_to_overwrite(no_ambient_key, capsys):
    key = str(no_ambient_key / "key")
    assert main(["record", "keygen", "--key", key]) == 0
    assert main(["record", "keygen", "--key", key]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["record", "keygen", "--key", key, "--force"]) == 0
