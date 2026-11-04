"""Story 1.6b: the sealed envelope carries no credential bytes (AD-14, AD-16).

The pipeline hands the observer a redacted pre-image; the envelope hashes
that, not the display content. Even a step whose display content still held
the token (a producer that forgot to redact its copy) would not put it into
the signed bytes — and the verifier still accepts the envelope.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeDB, _step_result
from emerge.preimage import output_preimage, redact_credentials
from emerge.run_attestation import canonical_json_bytes, verify_run_attestation
from validator.run_observer import RunAttestationObserver

CANARY = "canary-8f3a1c9e-connection-token-never-leaks"
CONNECTION = "did:orcha:agent:gh-1a2b3c4d"


async def _seal(*records) -> dict[str, Any]:
    observer = RunAttestationObserver(db=FakeDB())
    for record in records:
        await observer.on_step_complete(record)
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


@pytest.mark.asyncio
async def test_the_envelope_has_no_credential_bytes() -> None:
    raw = {"content": [{"type": "text", "text": f"headers=Bearer {CANARY}"}]}
    preimage = redact_credentials(
        output_preimage(raw), [("Authorization", f"Bearer {CANARY}")]
    )
    clean = _step_result("c1", agent_id=CONNECTION, content="[REDACTED:Authorization]")
    clean.capability_id = "search_repos"
    clean.output_preimage = preimage
    # control: a display copy that still carries the token
    careless = _step_result("c2", agent_id=CONNECTION, content=f"Bearer {CANARY}")
    careless.capability_id = "list_issues"
    careless.output_preimage = preimage

    envelope = await _seal(clean, careless)
    assert verify_run_attestation(envelope).valid
    canonical = canonical_json_bytes(envelope).decode()
    assert "[REDACTED:Authorization]" in json.dumps(preimage)  # positive control
    assert CANARY not in canonical
    assert CANARY not in json.dumps(envelope, default=str)


@pytest.mark.asyncio
async def test_without_a_preimage_the_display_content_is_what_is_hashed() -> None:
    # The fallback the pipeline uses when the SDK is absent: the observer
    # hashes ``content``. That is exactly why the pipeline redacts the
    # display copy too (test_canary.py in the SuperAgent suite).
    record = _step_result("c3", agent_id=CONNECTION, content="[REDACTED:Authorization]")
    record.capability_id = "search_repos"
    envelope = await _seal(record)
    assert verify_run_attestation(envelope).valid
    assert CANARY not in canonical_json_bytes(envelope).decode()
