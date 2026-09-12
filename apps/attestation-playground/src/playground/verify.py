"""Verify endpoint logic — thin wrapper over the SDK's vendored RFC 0003 verifier.

HARD RULE: verification logic lives only in emerge.run_attestation. This
module never re-implements canonicalization, chain, or signature checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from emerge.run_attestation import verify_run_attestation

REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = (
    REPO_ROOT / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"
)

_CHECK_REASONS = {
    "schema": "envelope failed schema validation",
    "steps_root": "step hash chain does not match steps_root",
    "signature": "Ed25519 signature does not verify",
}


def verify_envelope(envelope: Any) -> dict[str, Any]:
    """Verify an envelope via the SDK verifier and shape the API response."""
    if not isinstance(envelope, dict):
        return {
            "ok": False,
            "verdict": "invalid",
            "checks": {"schema": False, "steps_root": False, "signature": False},
            "run_id": None,
            "signer_did": None,
            "step_count": None,
            "reason": "envelope must be a JSON object",
        }
    verdict = verify_run_attestation(envelope)
    failed = [name for name, passed in verdict.checks.items() if not passed]
    reason = (
        "ok" if verdict.valid else _CHECK_REASONS.get(failed[0], "verification failed")
    )
    return {
        "ok": verdict.valid,
        "verdict": verdict.verdict,
        "checks": verdict.checks,
        "run_id": verdict.run_id,
        "signer_did": verdict.signer_did,
        "step_count": verdict.step_count,
        "reason": reason,
    }


def load_golden() -> dict[str, Any]:
    """Load the shared RFC 0003 golden vector (valid + tampered envelopes)."""
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
