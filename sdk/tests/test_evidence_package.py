"""Story 3.2 — the evidence package writes and re-verifies offline.

The module under test lives at ``scripts/kya_evidence_package.py`` and is
loaded by path, the way the golden-vector generator is. Everything here runs
without a stack: the envelopes come from the RFC 0003 golden vectors, and
the records are what the settlement gate would have written for them.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "kya_evidence_package.py"


@pytest.fixture(scope="module")
def pkg():
    spec = importlib.util.spec_from_file_location("kya_evidence_package", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def synthetic(pkg, tmp_path: Path) -> Path:
    package = tmp_path / "package"
    pkg.synthesize_package(package, reason="unit test", head="deadbeef")
    return package


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ── the happy path, declared synthetic ───────────────────────────────────────


def test_synthetic_package_verifies_and_says_it_is_synthetic(pkg, synthetic):
    report = pkg.verify_package(synthetic)
    assert report.present and report.ok
    assert report.synthetic is True
    assert report.synthetic_reason == "unit test"
    assert report.head == "deadbeef"
    assert report.legs == 2
    assert report.failures == []
    assert any("SYNTHETIC" in ln for ln in report.lines)
    manifest = _load(synthetic / "manifest.json")
    assert manifest["format"] == pkg.PACKAGE_FORMAT
    assert manifest["source"] == "synthetic"
    assert [leg["expected"] for leg in manifest["legs"]] == ["settled", "refused"]
    for name in (
        "leg1-envelope.json",
        "leg1-settlement.json",
        "leg2-envelope.json",
        "leg2-settlement.json",
    ):
        assert (synthetic / name).is_file()


def test_cli_verify_exit_codes_and_synthesize(pkg, tmp_path: Path, capsys):
    package = tmp_path / "cli"
    assert pkg.main(["synthesize", str(package), "--reason", "cli test"]) == 0
    assert pkg.main(["verify", str(package)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "synthetic" in out
    (package / "leg1-envelope.json").unlink()
    assert pkg.main(["verify", str(package)]) == 1


# ── every way the package can lie, caught offline ────────────────────────────


def test_a_tampered_valid_envelope_fails_the_settled_leg(pkg, synthetic):
    envelope = _load(synthetic / "leg1-envelope.json")
    envelope["steps"][0]["latency_ms"] += 1
    _dump(synthetic / "leg1-envelope.json", envelope)
    report = pkg.verify_package(synthetic)
    assert report.present and not report.ok
    assert any(
        "leg 1" in f and "does not reference this envelope" in f
        for f in report.failures
    )
    assert any("leg 1" in f and "expected settled" in f for f in report.failures)


def test_a_record_that_does_not_reference_its_envelope_fails(pkg, synthetic):
    record = _load(synthetic / "leg2-settlement.json")
    record["envelope_digest"] = "0" * 64
    _dump(synthetic / "leg2-settlement.json", record)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any(
        "leg 2" in f and "does not reference this envelope" in f
        for f in report.failures
    )


def test_a_refused_record_naming_the_wrong_verifier_check_fails(pkg, synthetic):
    record = _load(synthetic / "leg2-settlement.json")
    record["failed_checks"] = ["signature"]  # the verifier stops at steps_root
    _dump(synthetic / "leg2-settlement.json", record)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any(
        "verifier fails on 'steps_root', record names 'signature'" in f
        for f in report.failures
    )


def test_a_refused_record_claiming_a_settlement_fails(pkg, synthetic):
    record = _load(synthetic / "leg2-settlement.json")
    record["settled_run_id"] = record["run_id"]
    _dump(synthetic / "leg2-settlement.json", record)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any("claims settled_run_id" in f for f in report.failures)


def test_outcome_mismatch_with_the_manifest_fails(pkg, synthetic):
    record = _load(synthetic / "leg1-settlement.json")
    record["outcome"] = "refused"
    _dump(synthetic / "leg1-settlement.json", record)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any(
        "record outcome 'refused', manifest expects 'settled'" in f
        for f in report.failures
    )


def test_unknown_check_vocabulary_fails(pkg, synthetic):
    record = _load(synthetic / "leg2-settlement.json")
    record["failed_checks"] = ["steps_root", "vibes"]
    _dump(synthetic / "leg2-settlement.json", record)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any("unknown checks ['vibes'] (AR-12)" in f for f in report.failures)


def test_a_foreign_envelope_format_fails(pkg, synthetic):
    envelope = _load(synthetic / "leg1-envelope.json")
    envelope["format"] = "orcha.run-attestation/v2"
    _dump(synthetic / "leg1-envelope.json", envelope)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert any(
        "envelope format 'orcha.run-attestation/v2' (AR-12)" in f
        for f in report.failures
    )


# ── provenance: dual-run vs synthetic can never be confused ──────────────────


def test_a_synthetic_package_without_a_reason_cannot_be_written(pkg, tmp_path: Path):
    with pytest.raises(ValueError, match="synthetic_reason"):
        pkg.write_package(tmp_path / "p", source="synthetic", legs=[])
    with pytest.raises(ValueError, match="cannot carry synthetic_reason"):
        pkg.write_package(
            tmp_path / "q", source="dual-run", synthetic_reason="x", legs=[]
        )
    with pytest.raises(ValueError, match="source must be"):
        pkg.write_package(tmp_path / "r", source="demo", legs=[])


def test_a_manifest_relabelled_as_dual_run_is_caught(pkg, synthetic):
    """Editing the manifest to claim a live run, while keeping the reason, fails;
    dropping the reason too makes it indistinguishable by the manifest alone —
    the environment record and slice.json are what item 1 then demands."""
    manifest = _load(synthetic / "manifest.json")
    manifest["source"] = "dual-run"
    _dump(synthetic / "manifest.json", manifest)
    report = pkg.verify_package(synthetic)
    assert not report.ok
    assert report.synthetic is False
    assert any(
        "dual-run package carries synthetic_reason" in f for f in report.failures
    )


def test_absent_package_and_missing_envelopes_are_not_present(
    pkg, tmp_path: Path, synthetic
):
    report = pkg.verify_package(tmp_path / "nowhere")
    assert not report.present and not report.ok
    assert any("no readable manifest.json" in f for f in report.failures)

    (synthetic / "leg1-envelope.json").unlink()
    (synthetic / "leg2-envelope.json").unlink()
    report = pkg.verify_package(synthetic)
    assert not report.present and not report.ok
    assert sum("missing or unreadable" in f for f in report.failures) == 2


# ── writing from what the slice actually has in hand ─────────────────────────


def test_write_package_projects_orm_rows(pkg, tmp_path: Path):
    """The slice passes ORM rows: a Json-wrapped failed_checks and a datetime."""
    vectors = pkg._load_generator().build_vectors()
    valid, tampered = vectors["valid"], vectors["tampered"]
    when = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
    settled = SimpleNamespace(
        run_id=valid["run_id"],
        session_id="sess-1",
        outcome="settled",
        failed_checks=SimpleNamespace(data=[]),
        envelope_digest=pkg.compute_envelope_digest(valid),
        charter_hash=valid["charter_hash"],
        settled_run_id=valid["run_id"],
        call_id="call-1",
        created_at=when,
    )
    refused = SimpleNamespace(
        run_id=tampered["run_id"],
        session_id="sess-1",
        outcome="refused",
        failed_checks='["steps_root"]',
        envelope_digest=pkg.compute_envelope_digest(tampered),
        charter_hash=tampered["charter_hash"],
        settled_run_id=None,
        call_id=None,
        created_at=when,
    )
    package = tmp_path / "live"
    pkg.write_package(
        package,
        source="dual-run",
        head="abc123",
        legs=[(valid, settled, "settled"), (tampered, refused, "refused")],
    )
    record = _load(package / "leg1-settlement.json")
    assert record["failed_checks"] == []
    assert record["created_at"] == "2026-09-05T04:00:00+00:00"
    assert record["call_id"] == "call-1"
    assert _load(package / "leg2-settlement.json")["failed_checks"] == ["steps_root"]
    report = pkg.verify_package(package)
    assert report.ok and report.synthetic is False and report.source == "dual-run"


def test_envelope_files_are_the_envelopes_verbatim(pkg, synthetic):
    """A reader may hand the file straight to `orcha-sdk verify`: no wrapping."""
    from emerge.run_attestation import verify_run_attestation

    valid = _load(synthetic / "leg1-envelope.json")
    tampered = _load(synthetic / "leg2-envelope.json")
    assert verify_run_attestation(valid).valid is True
    assert verify_run_attestation(copy.deepcopy(tampered)).valid is False
