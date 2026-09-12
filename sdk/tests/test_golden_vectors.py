"""The RFC 0003 golden vectors are generated, not transcribed — and the SDK
verifier enforces the charset rule those vectors rely on.

``docs/spec/test-vectors/generate_run_attestation_golden.py`` derives every
hash, both step-commitment roots, the digest and the signature from the RFC's
example inputs and the example-only seed. This module runs that generator
in-process and asserts the committed file is exactly what it produces, so a
hand edit to the vectors — or a verifier change the generator does not
reflect — fails here instead of sitting green.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from emerge.run_attestation import verify_run_attestation

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_DIR = REPO_ROOT / "docs" / "spec" / "test-vectors"
GOLDEN_PATH = VECTORS_DIR / "run-attestation-golden.json"
GENERATOR_PATH = VECTORS_DIR / "generate_run_attestation_golden.py"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location(
        "generate_run_attestation_golden", GENERATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ── Reproducibility ──────────────────────────────────────────────────────────


def test_committed_vectors_are_exactly_what_the_generator_emits(generator):
    """Byte-for-byte, including formatting — regenerate, never hand-edit."""
    assert GOLDEN_PATH.read_text(encoding="utf-8") == generator.render(
        generator.build_vectors()
    )


def test_generator_check_mode_agrees(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "reproducible" in capsys.readouterr().out


def test_generator_print_example_is_the_valid_vector(generator, golden, capsys):
    assert generator.main(["--print-example"]) == 0
    assert json.loads(capsys.readouterr().out) == golden["valid"]


def test_tampered_vector_breaks_every_commitment(generator, golden):
    """The tamper must fail BOTH roots and the signature — each on its own.

    The verifier stops at the first failure, so its verdict alone cannot show
    that all three commitments noticed. A tamper that broke only one would be
    a regression in the vector, not a pass.
    """
    tampered = golden["tampered"]
    assert generator.compute_steps_root(tampered["steps"]) != tampered["steps_root"]
    assert (
        generator.compute_steps_merkle_root(tampered["steps"])
        != tampered["steps_merkle_root"]
    )
    assert generator.compute_envelope_digest(
        tampered
    ) != generator.compute_envelope_digest(golden["valid"])
    verdict = verify_run_attestation(tampered)
    assert verdict.valid is False
    assert verdict.checks["schema"] is True


# ── The charset rule, as the SDK verifier enforces it ────────────────────────


def _with(envelope: dict, path: tuple, value) -> dict:
    mutated = copy.deepcopy(envelope)
    target = mutated
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return mutated


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("run_id",), "run-é"),
        (("run_id",), ""),
        (("policy_version",), "example-policy/é"),
        (("agent_dids", 0), "did:orcha:agent:ü"),
        (("steps", 0, "call_id"), "call-é"),
        (("steps", 0, "tool"), "résumé"),
        (("steps", 0, "tool"), "tab\there"),
        (("steps", 0, "tool"), "del\x7f"),
        (("verdicts", 0, "check"), "ç"),
        (("verdicts", 0, "detail"), "é"),
        (("signer", "did"), "did:orcha:system:é"),
        (("signer", "public_key_b64"), "not base64!"),
        (("signature",), "not base64!"),
        (("steps", 1, "cdv_bp"), 1001),
        (("steps", 1, "cdv_bp"), -1),
        (("steps", 1, "cdv_bp"), 0.91),
        (("steps", 1, "cdv_bp"), True),
    ],
)
def test_schema_rejects_values_outside_the_charset_rule(golden, path, value):
    verdict = verify_run_attestation(_with(golden["valid"], path, value))
    assert verdict.valid is False
    assert verdict.checks["schema"] is False


def test_schema_rejects_the_retired_float_field(golden):
    step = {**golden["valid"]["steps"][1]}
    del step["cdv_bp"]
    step["cdv_score"] = 0.91
    envelope = _with(golden["valid"], ("steps", 1), step)
    assert verify_run_attestation(envelope).checks["schema"] is False


def test_empty_detail_and_boundary_cdv_bp_pass_schema(golden):
    """The rule's edges: empty detail is allowed, 0 and 1000 are in range.

    These mutations break the roots and the signature, so only the schema
    check is asserted — that is the check under test.
    """
    verdicts = [{"check": "authorized_scope", "result": "pass", "detail": ""}]
    assert verify_run_attestation(
        _with(golden["valid"], ("verdicts",), verdicts)
    ).checks["schema"]
    for bp in (0, 1000):
        assert verify_run_attestation(
            _with(golden["valid"], ("steps", 1, "cdv_bp"), bp)
        ).checks["schema"]


# ── DID syntax is envelope-level; the did:orcha method is the platform profile ─


@pytest.mark.parametrize(
    "did",
    [
        "did:web:example.com",
        "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
        "did:orcha:agent:example-support-bot",
        "did:web:example.com:user:alice",
    ],
)
def test_schema_accepts_general_did_syntax(golden, did):
    """These mutations break the signature; only the schema check is asserted."""
    for path in (("agent_dids", 0), ("signer", "did")):
        assert verify_run_attestation(_with(golden["valid"], path, did)).checks[
            "schema"
        ]


@pytest.mark.parametrize(
    "did",
    [
        "",
        "did:",
        "did:web",
        "did:Web:x",
        "DID:web:x",
        "web:x",
        "did:web:\u00fc",
        "did:web:x\x7f",
    ],
)
def test_schema_rejects_malformed_dids(golden, did):
    for path in (("agent_dids", 0), ("signer", "did")):
        verdict = verify_run_attestation(_with(golden["valid"], path, did))
        assert verdict.checks["schema"] is False
