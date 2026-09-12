#!/usr/bin/env python3
"""Evidence package for the KYA dual run — write it, verify it offline (Story 3.2).

A package is a directory carrying, for each leg of the dual run, the signed
envelope and the ``attested_settlements`` audit record, plus a manifest that
says where they came from. Verification is fully offline and uses the
vendored verifier alone (``emerge.run_attestation``): the valid leg must
verify green, the tampered leg must verify red on the check its record
names, and every record must reference its envelope by digest (FR-11,
NFR-2).

Layout::

    <package>/
      manifest.json           format, source, legs (files + expectations)
      leg1-envelope.json      orcha.run-attestation/v1
      leg1-settlement.json    the audit row: outcome, failed_checks, digest…
      leg2-envelope.json
      leg2-settlement.json
      slice.json, environment.json, leg1-sse.txt   (written by the slice)

``manifest.source`` is ``"dual-run"`` only when the slice wrote the package
from a live run. A package built from generated envelopes says
``"synthetic"`` and carries ``synthetic_reason``; it exercises the packaging
and re-verify mechanism and is never evidence that the dual run happened.
A verifier that could not tell the two apart would be lying in exactly the
direction that matters.

Vocabulary (AR-12): the only envelope format is ``orcha.run-attestation/v1``;
verifier check keys are those RFC 0003 defines (``schema``, ``steps_root``,
``steps_merkle_root``, ``signature`` — the epic's list predates the Merkle
root); a record's ``failed_checks`` may additionally use the settlement
gate's own named checks.

CLI::

    python scripts/kya_evidence_package.py verify <package>        exit 0 iff ok
    python scripts/kya_evidence_package.py synthesize <package> --reason "…"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.run_attestation import (
    RUN_ATTESTATION_FORMAT,
    compute_envelope_digest,
    verify_run_attestation,
)

PACKAGE_FORMAT = "orcha.kya-evidence/v1"
MANIFEST = "manifest.json"
SOURCES = ("dual-run", "synthetic")
VERIFIER_CHECKS = ("schema", "steps_root", "steps_merkle_root", "signature")
GATE_CHECKS = (
    "missing_attestation",
    "run_id_mismatch",
    "charter_hash",
    "verify_error",
    "signer_did",
    "agent_did",
    "audit_write_error",
    "already_settled",
    "credit_write_error",
)
RECORD_FIELDS = (
    "run_id",
    "outcome",
    "failed_checks",
    "envelope_digest",
    "charter_hash",
    "settled_run_id",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = (
    _REPO_ROOT / "docs" / "spec" / "test-vectors" / "generate_run_attestation_golden.py"
)


# ── writing ──────────────────────────────────────────────────────────────────


def _record_from_row(row: Any) -> dict[str, Any]:
    """Project an ``attested_settlements`` row (ORM object or dict) to JSON."""
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    checks = get("failed_checks")
    checks = getattr(checks, "data", checks)
    if isinstance(checks, str):
        checks = json.loads(checks)
    created = get("created_at")
    return {
        "run_id": get("run_id"),
        "session_id": get("session_id"),
        "outcome": get("outcome"),
        "failed_checks": list(checks or []),
        "envelope_digest": get("envelope_digest"),
        "charter_hash": get("charter_hash"),
        "settled_run_id": get("settled_run_id"),
        "call_id": get("call_id"),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def write_package(
    package: Path,
    *,
    source: str,
    legs: list[tuple[dict[str, Any], Any, str]],
    head: str | None = None,
    synthetic_reason: str | None = None,
) -> Path:
    """Write envelopes, records and the manifest. ``legs`` is a list of
    ``(envelope, settlement_row, expected_outcome)`` in leg order."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if source == "synthetic" and not synthetic_reason:
        raise ValueError("a synthetic package must state synthetic_reason")
    if source == "dual-run" and synthetic_reason:
        raise ValueError("a dual-run package cannot carry synthetic_reason")
    package.mkdir(parents=True, exist_ok=True)
    manifest_legs: list[dict[str, Any]] = []
    for index, (envelope, row, expected) in enumerate(legs, start=1):
        if expected not in ("settled", "refused"):
            raise ValueError(
                f"expected outcome must be settled|refused, got {expected!r}"
            )
        env_name, rec_name = f"leg{index}-envelope.json", f"leg{index}-settlement.json"
        (package / env_name).write_text(
            json.dumps(envelope, indent=2) + "\n", encoding="utf-8"
        )
        record = _record_from_row(row)
        (package / rec_name).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        manifest_legs.append(
            {
                "leg": index,
                "expected": expected,
                "run_id": envelope.get("run_id"),
                "envelope": env_name,
                "record": rec_name,
            }
        )
    manifest = {
        "format": PACKAGE_FORMAT,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "head": head,
        "verifier": "emerge.run_attestation (vendored, offline)",
        "legs": manifest_legs,
    }
    if synthetic_reason:
        manifest["synthetic_reason"] = synthetic_reason
    (package / MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return package / MANIFEST


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_run_attestation_golden", _GENERATOR
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def synthesize_package(package: Path, *, reason: str, head: str | None = None) -> Path:
    """A package built from the RFC 0003 golden vectors, declared synthetic.

    The records are what the settlement gate would have written for these
    envelopes: the valid one settled, the tampered one refused on
    ``steps_root`` (the first commitment the tamper breaks).
    """
    vectors = _load_generator().build_vectors()
    valid, tampered = vectors["valid"], vectors["tampered"]
    settled_row = {
        "run_id": valid["run_id"],
        "session_id": "synthetic",
        "outcome": "settled",
        "failed_checks": [],
        "envelope_digest": compute_envelope_digest(valid),
        "charter_hash": valid.get("charter_hash"),
        "settled_run_id": valid["run_id"],
        "call_id": None,
        "created_at": None,
    }
    refused_row = {
        **settled_row,
        "run_id": tampered["run_id"],
        "outcome": "refused",
        "failed_checks": ["steps_root"],
        "envelope_digest": compute_envelope_digest(tampered),
        "settled_run_id": None,
    }
    return write_package(
        package,
        source="synthetic",
        synthetic_reason=reason,
        head=head,
        legs=[(valid, settled_row, "settled"), (tampered, refused_row, "refused")],
    )


# ── verifying ────────────────────────────────────────────────────────────────


@dataclass
class PackageReport:
    """What an offline reader can say about a package."""

    package: Path
    present: bool = False  # a manifest and at least one envelope exist
    synthetic: bool = False
    synthetic_reason: str | None = None
    source: str | None = None
    head: str | None = None
    ok: bool = False  # every leg verified as expected; vocabulary clean
    legs: int = 0
    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        self.lines.append(f"FAIL  {msg}")

    def note(self, msg: str) -> None:
        self.lines.append(f"ok    {msg}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_package(package: Path) -> PackageReport:
    """Offline verification with the vendored verifier alone. Never raises."""
    report = PackageReport(package=package)
    manifest = _read_json(package / MANIFEST) if package.is_dir() else None
    if not isinstance(manifest, dict):
        report.fail(f"no readable {MANIFEST} in {package}")
        return report
    if manifest.get("format") != PACKAGE_FORMAT:
        report.fail(
            f"manifest format {manifest.get('format')!r} is not {PACKAGE_FORMAT}"
        )
        return report
    source = manifest.get("source")
    report.source = source if isinstance(source, str) else None
    report.head = (
        manifest.get("head") if isinstance(manifest.get("head"), str) else None
    )
    if source not in SOURCES:
        report.fail(f"manifest source {source!r} is not one of {SOURCES}")
    report.synthetic = source == "synthetic"
    report.synthetic_reason = manifest.get("synthetic_reason")
    if report.synthetic and not report.synthetic_reason:
        report.fail("synthetic package states no synthetic_reason")
    if source == "dual-run" and manifest.get("synthetic_reason"):
        report.fail("a dual-run package carries synthetic_reason")

    legs = manifest.get("legs")
    if not isinstance(legs, list) or not legs:
        report.fail("manifest lists no legs")
        return report
    report.legs = len(legs)
    envelopes_seen = 0
    for entry in legs:
        leg = entry.get("leg") if isinstance(entry, dict) else None
        label = f"leg {leg}"
        if not isinstance(entry, dict):
            report.fail(f"{label}: manifest entry is not an object")
            continue
        expected = entry.get("expected")
        envelope = _read_json(package / str(entry.get("envelope")))
        record = _read_json(package / str(entry.get("record")))
        if not isinstance(envelope, dict):
            report.fail(
                f"{label}: envelope file {entry.get('envelope')!r} missing or unreadable"
            )
            continue
        envelopes_seen += 1
        if envelope.get("format") != RUN_ATTESTATION_FORMAT:
            report.fail(f"{label}: envelope format {envelope.get('format')!r} (AR-12)")
        if envelope.get("run_id") != entry.get("run_id"):
            report.fail(
                f"{label}: envelope run_id {envelope.get('run_id')!r} != manifest"
            )
        if not isinstance(record, dict):
            report.fail(
                f"{label}: record file {entry.get('record')!r} missing or unreadable"
            )
            continue
        missing = [k for k in RECORD_FIELDS if k not in record]
        if missing:
            report.fail(f"{label}: record lacks {missing}")
            continue

        verdict = verify_run_attestation(envelope)
        unknown = sorted(set(verdict.checks) - set(VERIFIER_CHECKS))
        if unknown:
            report.fail(
                f"{label}: verifier emitted unknown check keys {unknown} (AR-12)"
            )
        failed_now = [k for k in VERIFIER_CHECKS if verdict.checks.get(k) is False]
        record_checks = record.get("failed_checks") or []
        bad_vocab = [c for c in record_checks if c not in VERIFIER_CHECKS + GATE_CHECKS]
        if bad_vocab:
            report.fail(f"{label}: record names unknown checks {bad_vocab} (AR-12)")

        digest = compute_envelope_digest(envelope)
        if record.get("envelope_digest") != digest:
            report.fail(
                f"{label}: record digest {str(record.get('envelope_digest'))[:12]}… does not reference this envelope ({digest[:12]}…)"
            )
        if record.get("run_id") != envelope.get("run_id"):
            report.fail(f"{label}: record run_id {record.get('run_id')!r} != envelope")
        if record.get("outcome") != expected:
            report.fail(
                f"{label}: record outcome {record.get('outcome')!r}, manifest expects {expected!r}"
            )

        if expected == "settled":
            if (
                verdict.valid
                and not record_checks
                and record.get("settled_run_id") == envelope.get("run_id")
            ):
                report.note(
                    f"{label}: envelope verifies green offline; record settled, digest matches"
                )
            else:
                report.fail(
                    f"{label}: expected settled — verifier valid={verdict.valid}, "
                    f"record failed_checks={record_checks}, settled_run_id={record.get('settled_run_id')!r}"
                )
        elif expected == "refused":
            if verdict.valid:
                report.fail(
                    f"{label}: expected refused but the envelope verifies green"
                )
            elif (
                failed_now
                and record_checks
                and record_checks[0] != failed_now[0]
                and record_checks[0] in VERIFIER_CHECKS
            ):
                report.fail(
                    f"{label}: verifier fails on {failed_now[0]!r}, record names {record_checks[0]!r}"
                )
            elif record.get("settled_run_id") is not None:
                report.fail(f"{label}: a refused record claims settled_run_id")
            else:
                named = record_checks[0] if record_checks else "<none>"
                report.note(
                    f"{label}: envelope verifies red offline on {failed_now[0] if failed_now else '?'}; record refused naming {named}, digest matches"
                )
        else:
            report.fail(f"{label}: manifest expects {expected!r}, not settled|refused")

    report.present = envelopes_seen > 0
    report.ok = report.present and not report.failures
    if report.synthetic and report.ok:
        report.lines.append(
            f"      SYNTHETIC package ({report.synthetic_reason}) — mechanism only, not a dual-run result"
        )
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def _head() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — git by name, fixed argv
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser(
        "verify", help="verify a package offline; exit 0 iff every leg checks out"
    )
    v.add_argument("package", type=Path)
    s = sub.add_parser(
        "synthesize", help="build a package from the golden vectors, declared synthetic"
    )
    s.add_argument("package", type=Path)
    s.add_argument(
        "--reason",
        required=True,
        help="why this package is synthetic (recorded in the manifest)",
    )
    args = parser.parse_args(argv)

    if args.cmd == "synthesize":
        path = synthesize_package(args.package, reason=args.reason, head=_head())
        print(f"wrote {path} (source=synthetic)")
        return 0
    report = verify_package(args.package)
    for line in report.lines:
        print(line)
    print(
        f"package {args.package}: "
        + ("OK" if report.ok else "FAILED")
        + (" (synthetic — not a dual-run result)" if report.synthetic else "")
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
