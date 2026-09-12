"""Demo charter fixture pin (Story 1.2, AD-9).

The settling-path charter binding is ``sha256_hex(canonical_json_bytes(fixture))``
— never the raw file bytes (formatting drift would break the pin). The
expected hash below is the single source of truth; the fixture README points
here.
"""

from __future__ import annotations

import pytest
from conftest import FIXTURE_PATH, FakeDB, _step_result, load_demo_charter
from emerge_node.envelope import canonical_json_bytes
from validator.run_envelope import sha256_hex
from validator.run_observer import RunAttestationObserver

# sha256 hex of canonical_json_bytes(demo-charter.json) — pinned.
DEMO_CHARTER_HASH = "bb04f25c508bc4766bb27712da24017c50f618f9378890c1709f763a82f83a5d"


def _fixture_hash() -> str:
    return sha256_hex(canonical_json_bytes(load_demo_charter()))


def test_demo_charter_fixture_exists() -> None:
    fixture = load_demo_charter()
    assert FIXTURE_PATH.name == "demo-charter.json"
    assert fixture["format"] == "orcha.demo-charter/v1"
    assert fixture["agent_did"] == "did:orcha:agent:kya-demo"


def test_demo_charter_hash_is_pinned() -> None:
    assert _fixture_hash() == DEMO_CHARTER_HASH


# ── Observer charter binding (Story 1.2, AC1/AC2) ────────────────────────────


@pytest.mark.asyncio
async def test_observer_seals_envelope_with_configured_charter_hash() -> None:
    observer = RunAttestationObserver(db=FakeDB(), charter_hash=DEMO_CHARTER_HASH)
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_run_complete("sess-1")

    envelope = next(iter(observer.envelopes.values()))
    assert envelope["charter_hash"] == DEMO_CHARTER_HASH
    assert len(envelope["charter_hash"]) == 64


@pytest.mark.asyncio
async def test_observer_default_seals_charter_hash_null() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_step_complete(_step_result("c1"))
    await observer.on_run_complete("sess-1")

    envelope = next(iter(observer.envelopes.values()))
    assert "charter_hash" in envelope  # exact RFC 0003 spelling: null, never absent
    assert envelope["charter_hash"] is None
