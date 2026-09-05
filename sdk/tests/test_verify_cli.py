"""Tests for `emerge verify` — the offline run attestation verifier (RFC 0003).

Loads the shared golden vectors from
docs/spec/test-vectors/run-attestation-golden.json (same file consumed by
node/tests and services/validator/tests; shipped with the spec so the OSS
export keeps it).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from emerge.cli import main
from emerge.run_attestation import verify_run_attestation

GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "spec"
    / "test-vectors"
    / "run-attestation-golden.json"
)


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _write_tmp(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "envelope.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Golden vectors ──────────────────────────────────────────────────────────


def test_verify_golden_envelope_valid(golden, tmp_path, capsys):
    rc = main(["verify", str(_write_tmp(tmp_path, golden["valid"]))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VALID" in out
    assert "run-9c2e-example" in out  # run id
    assert "did:orcha:system:validator" in out  # signer DID
    assert "steps:     2" in out  # rendered step-count line (run id also contains "2")


def test_verify_tampered_envelope_invalid(golden, tmp_path, capsys):
    rc = main(["verify", str(_write_tmp(tmp_path, golden["tampered"]))])
    out = capsys.readouterr().out
    assert rc == 1
    assert "INVALID" in out


# ── Input handling ──────────────────────────────────────────────────────────


def test_verify_reads_envelope_from_stdin(golden, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(golden["valid"])))
    rc = main(["verify", "-"])
    assert rc == 0
    assert "VALID" in capsys.readouterr().out


def test_verify_malformed_json_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    rc = main(["verify", "-"])
    assert rc == 2
    assert "json" in capsys.readouterr().err.lower()


def test_verify_missing_file_is_usage_error(capsys):
    rc = main(["verify", "/nonexistent/envelope.json"])
    assert rc == 2
    assert capsys.readouterr().err  # explains the failure


def test_verify_non_object_json_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
    rc = main(["verify", "-"])
    assert rc == 2


# ── --json output ───────────────────────────────────────────────────────────


def test_verify_json_output_valid(golden, tmp_path, capsys):
    rc = main(["verify", "--json", str(_write_tmp(tmp_path, golden["valid"]))])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["valid"] is True
    assert result["verdict"] == "valid"
    assert result["run_id"] == "run-9c2e-example"
    assert result["signer_did"] == "did:orcha:system:validator"
    assert result["step_count"] == 2
    assert result["checks"] == {
        "schema": True,
        "steps_root": True,
        "steps_merkle_root": True,
        "signature": True,
    }


def test_verify_json_output_tampered(golden, tmp_path, capsys):
    rc = main(["verify", "--json", str(_write_tmp(tmp_path, golden["tampered"]))])
    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result["valid"] is False
    assert result["verdict"] == "invalid"
    assert result["checks"]["schema"] is True
    assert result["checks"]["steps_root"] is False
    assert result["checks"]["steps_merkle_root"] is False
    assert result["checks"]["signature"] is False


# ── --resolve-did: opt-in, explicitly unsupported for now ───────────────────


def test_verify_resolve_did_fails_with_clear_message(golden, tmp_path, capsys):
    rc = main(["verify", "--resolve-did", str(_write_tmp(tmp_path, golden["valid"]))])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not yet supported" in err


# ── Help documents exit codes ────────────────────────────────────────────────


def test_verify_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["verify", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "0" in out and "1" in out and "2" in out
    assert "valid" in out.lower()


# ── Story 1.3: offline verify parity on JSONB-reordered bytes (AR-17) ───────


# Scramble twin lives in services/validator/tests/test_readback_parity.py —
# keep the two in sync.
def _scramble_keys(value):
    """Deterministically reverse every dict's key order, recursing through
    lists WITHOUT reordering list elements (JSONB-reorder simulation)."""
    if isinstance(value, dict):
        return {key: _scramble_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_scramble_keys(item) for item in value]
    return value


def test_scramble_actually_mutates_key_order() -> None:
    scrambled = _scramble_keys({"a": 1, "b": 2, "c": 3})
    assert list(scrambled) == ["c", "b", "a"]
    # Lists are traversed, never reordered.
    assert _scramble_keys({"s": [{"x": 1, "y": 2}, {"z": 3}]}) == {
        "s": [{"y": 2, "x": 1}, {"z": 3}]
    }


def test_sdk_verify_valid_despite_key_reorder(golden):
    scrambled = _scramble_keys(golden["valid"])
    assert list(scrambled) != list(golden["valid"])  # real mutation
    verdict = verify_run_attestation(scrambled)
    assert verdict.valid is True
    assert verdict.checks == {
        "schema": True,
        "steps_root": True,
        "steps_merkle_root": True,
        "signature": True,
    }


def test_cli_agrees_with_sdk_on_reordered_bytes(golden, tmp_path, capsys):
    scrambled = _scramble_keys(golden["valid"])
    sdk_verdict = verify_run_attestation(scrambled)

    rc = main(["verify", "--json", str(_write_tmp(tmp_path, scrambled))])
    cli_result = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert cli_result["valid"] is True
    assert cli_result["valid"] == sdk_verdict.valid
    assert cli_result["checks"] == sdk_verdict.checks


def test_reordered_and_tampered_still_refuses(golden, tmp_path, capsys):
    """Negative control: reorder is tolerated, tamper is not."""
    tampered = _scramble_keys(golden["tampered"])
    sdk_verdict = verify_run_attestation(tampered)
    assert sdk_verdict.valid is False
    assert sdk_verdict.checks["steps_root"] is False
    assert sdk_verdict.checks["steps_merkle_root"] is False

    rc = main(["verify", "--json", str(_write_tmp(tmp_path, tampered))])
    cli_result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert cli_result["valid"] == sdk_verdict.valid
    assert cli_result["checks"] == sdk_verdict.checks
