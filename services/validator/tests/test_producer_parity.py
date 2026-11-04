"""Story 4.1 (FR-27, AD-5, AD-20): one record, two producers, one verifier.

The same declared criteria sealed by the platform (the run observer, as the
pipeline feeds it) and by the plugin (``orcha record``'s journal) must give
envelopes that carry the same criteria digest in ``policy_version``, the same
``declared_acceptance`` verdict for the same step outputs, and verify under
the one published verifier; and no code path may branch on which producer
sealed an envelope. A producer change that breaks any of this fails here.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from conftest import FakeDB, _step_result
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge import criteria as sdk_criteria
from emerge import record as sdk_record
from emerge import run_attestation as sdk_verify
from emerge.criteria import (
    criteria_digest,
    parse_policy_version,
)
from emerge.journal import append_step, new_journal, seal_journal
from emerge.preimage import output_preimage
from emerge.run_attestation import verify_run_attestation
from superagent.middleware import criteria as platform_criteria
from superagent.middleware.criteria import agent_step_meta
from validator import run_envelope, run_observer
from validator.run_observer import RunAttestationObserver

CRITERIA = {"exit_zero": True, "citations_required": False}
POLICY = "run-attestation/1.0"
OUTPUTS: list[Any] = [
    {"stdout": "read a file"},  # n/a for exit_zero
    {"exit_code": 0, "stdout": "9 passed"},
    {"exit_code": 3, "stdout": "1 failed"},
]


async def _bot_seal(outputs: list[Any], *, failed: bool = False) -> dict[str, Any]:
    """Seal as the platform does: the pipeline's step metadata (the function
    it stamps on successful, failed and declined steps), the observer's seal."""
    observer = RunAttestationObserver(db=FakeDB(), policy_version=POLICY)
    for index, raw in enumerate(outputs):
        await observer.on_step_complete(
            _step_result(
                f"c{index}",
                tool_name="run_tests",
                success=not failed,
                content=raw if isinstance(raw, str) else json.dumps(raw),
                output_preimage=output_preimage(raw),
                metadata=agent_step_meta(CRITERIA, None, raw),
            )
        )
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


def _plugin_seal(
    outputs: list[Any], key: Ed25519PrivateKey, *, failed: bool = False
) -> dict[str, Any]:
    """Seal as ``orcha record`` does: journaled raw steps, sealed on Stop."""
    journal = new_journal(
        run_id="sess-1",
        agent_dids=[],
        policy=POLICY,
        criteria=CRITERIA,
        started_at="2026-10-05T10:00:00Z",
    )
    for index, raw in enumerate(outputs):
        append_step(
            journal,
            {
                "call_id": f"c{index}",
                "tool": "claude-code/Bash",
                "args": {"command": "pytest -q"},
                "output": raw,
                "success": not failed,
                "latency_ms": 5,
            },
        )
    return seal_journal(journal, key, finished_at="2026-10-05T10:00:05Z")


@pytest.mark.asyncio
async def test_the_same_criteria_give_the_same_digest_and_verdict_in_both_producers():
    bot = await _bot_seal(OUTPUTS)
    plugin = _plugin_seal(OUTPUTS, Ed25519PrivateKey.generate())

    # both verify under the one verifier, each against its own signer
    assert verify_run_attestation(bot).valid
    assert verify_run_attestation(plugin).valid
    assert bot["signer"]["did"] != plugin["signer"]["did"]

    # the criteria digest in policy_version is byte-identical
    expected = criteria_digest(CRITERIA)
    assert parse_policy_version(bot["policy_version"]) == (POLICY, expected)
    assert parse_policy_version(plugin["policy_version"]) == (POLICY, expected)

    # the declared_acceptance verdict is the same rule applied to the same bytes
    bot_verdicts = [v for v in bot["verdicts"] if v["check"] == "declared_acceptance"]
    plugin_verdicts = [
        v for v in plugin["verdicts"] if v["check"] == "declared_acceptance"
    ]
    assert bot_verdicts == plugin_verdicts
    assert bot_verdicts == [
        {
            "check": "declared_acceptance",
            "result": "fail",
            "detail": "exit_zero: nonzero exit: 3",
        }
    ]

    # and each step commits to the same output bytes (same pre-image, same hash)
    assert [s["output_hash"] for s in bot["steps"]] == [
        s["output_hash"] for s in plugin["steps"]
    ]


@pytest.mark.asyncio
async def test_a_run_of_only_failed_calls_fails_its_acceptance_in_both_producers():
    # the platform used to stamp no entry on a failed dispatch, so such a run
    # signed no declared_acceptance where the journal signed a fail
    errors = ["Error: connection refused", "Error: timeout"]
    bot = await _bot_seal(errors, failed=True)
    plugin = _plugin_seal(errors, Ed25519PrivateKey.generate(), failed=True)
    expected = {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "no step reported an exit code",
    }
    assert expected in bot["verdicts"] and expected in plugin["verdicts"]
    assert [s["output_hash"] for s in bot["steps"]] == [
        s["output_hash"] for s in plugin["steps"]
    ]
    assert verify_run_attestation(bot).valid and verify_run_attestation(plugin).valid


@pytest.mark.asyncio
async def test_a_passing_run_reads_the_same_in_both_producers():
    passing = [{"exit_code": 0}, {"stdout": "x"}]
    bot = await _bot_seal(passing)
    plugin = _plugin_seal(passing, Ed25519PrivateKey.generate())
    verdict = {"check": "declared_acceptance", "result": "pass", "detail": "ok"}
    assert verdict in bot["verdicts"] and verdict in plugin["verdicts"]
    assert verify_run_attestation(bot).valid and verify_run_attestation(plugin).valid


def test_there_is_one_implementation_and_the_platform_reads_it():
    # AD-20: the validator builds and signs through the SDK's functions,
    # not copies; the platform's criteria module re-exports the SDK's names
    assert run_envelope._build_run_envelope is sdk_record.build_run_envelope
    assert run_envelope._sign_run_envelope is sdk_record.sign_run_envelope
    assert run_envelope.compute_steps_root is sdk_verify.compute_steps_root
    assert run_envelope.compute_envelope_digest is sdk_verify.compute_envelope_digest
    assert run_envelope.canonical_json_bytes is sdk_verify.canonical_json_bytes
    assert run_observer.compose_policy_version is sdk_criteria.compose_policy_version
    assert run_observer.run_declared_acceptance is sdk_criteria.run_declared_acceptance
    assert platform_criteria.criteria_digest is sdk_criteria.criteria_digest
    assert platform_criteria.step_declared_acceptance is (
        sdk_criteria.step_declared_acceptance
    )
    assert platform_criteria.criteria_units is sdk_criteria.criteria_units
    with pytest.raises(ImportError):
        import validator.criteria  # noqa: F401 — deleted (AD-20)


def test_the_verifier_never_branches_on_the_producer():
    # NFR-1 / AD-5: nothing in the verifier reads who sealed the envelope
    # beyond the signer key it checks the signature against
    bodies = "".join(
        inspect.getsource(fn)
        for fn in (
            sdk_verify.verify_run_attestation,
            sdk_verify._schema_ok,
            sdk_verify._verify_signature,
            sdk_verify.compute_envelope_digest,
        )
    )
    # no DID value is special-cased: the platform's namespace and the local
    # key's naming convention are both absent from the verifying code (the
    # signer DID is displayed in the verdict, never compared)
    for literal in ("did:orcha:system", "did:orcha:agent", "key-"):
        assert literal not in bodies, literal
    assert "signer_did ==" not in bodies and "signer_did !=" not in bodies
    assert ".startswith(" not in inspect.getsource(sdk_verify.verify_run_attestation)
    # the verifier's answer does not depend on the signer DID's namespace: the
    # same key, the same steps, sealed under the local and the platform
    # naming, both verify — which DIDs are trusted is the settle gate's
    # policy (its signer trust anchor), never the verifier's
    key = Ed25519PrivateKey.generate()
    public_key_b64 = sdk_record.public_key_b64_of(key)
    for did in ("did:orcha:agent:key-0123456789abcdef", "did:orcha:system:validator"):
        unsigned = sdk_record.build_run_envelope(
            run_id="sess-1",
            agent_dids=[],
            charter_hash=None,
            policy_version=POLICY,
            steps=[],
            verdicts=[],
            started_at="2026-10-05T10:00:00Z",
            finished_at="2026-10-05T10:00:05Z",
            signer_did=did,
            public_key_b64=public_key_b64,
        )
        verdict = verify_run_attestation(sdk_record.sign_run_envelope(unsigned, key))
        assert verdict.valid and verdict.signer_did == did
