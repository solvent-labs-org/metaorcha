"""Story 1.4: every connector call is a step in the receipt.

AD-17 names the connection inside ``tool`` as ``"<DID>#<capability>"`` and the
DID in ``agent_dids``; AD-16 makes ``output_hash`` commit to the raw, redacted
pre-image the pipeline hands over, not the display copy. Checked here with the
published SDK verifier, not the service's own.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeDB, _step_result
from emerge.preimage import output_preimage, redact_credentials
from emerge.run_attestation import (
    canonical_json_bytes,
    compute_steps_merkle_root,
    compute_steps_root,
    sha256_hex,
    verify_run_attestation,
)
from validator.run_observer import RunAttestationObserver, step_tool

CONNECTION = "did:orcha:agent:gh-connection"


def _connector_call(
    call_id: str,
    capability: str,
    *,
    preimage: Any,
    success: bool = True,
    content: str = "display copy",
) -> SimpleNamespace:
    record = _step_result(
        call_id,
        agent_id=CONNECTION,
        tool_name=f"gh__{capability}",
        content=content,
        success=success,
    )
    record.capability_id = capability
    record.output_preimage = preimage
    return record


async def _seal(*records: SimpleNamespace) -> dict[str, Any]:
    observer = RunAttestationObserver(db=FakeDB())
    for record in records:
        await observer.on_step_complete(record)
    await observer.on_run_complete("sess-1")
    (envelope,) = observer.envelopes.values()
    return envelope


def _hash(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


@pytest.mark.asyncio
async def test_three_connector_calls_are_three_attributed_rows() -> None:
    raw = [
        "x" * 600,  # longer than the 280-character display copy
        {"items": [{"name": "orcha"}], "total": 1},
        redact_credentials(
            output_preimage("token=tok-123"), {"GITHUB_TOKEN": "tok-123"}
        ),
    ]
    envelope = await _seal(
        _connector_call("c1", "search_repos", preimage=raw[0]),
        _connector_call("c2", "list_issues", preimage=raw[1]),
        _connector_call("c3", "get_repo", preimage=raw[2]),
    )

    assert verify_run_attestation(envelope).valid
    steps = envelope["steps"]
    assert [s["tool"] for s in steps] == [
        f"{CONNECTION}#search_repos",
        f"{CONNECTION}#list_issues",
        f"{CONNECTION}#get_repo",
    ]
    assert CONNECTION in envelope["agent_dids"]
    for step in steps:
        assert set(step) >= {"args_hash", "output_hash", "success", "latency_ms"}
    # output_hash is over the pre-image, never the display copy.
    assert [s["output_hash"] for s in steps] == [_hash(v) for v in raw]
    assert all(s["output_hash"] != _hash("display copy") for s in steps)
    assert raw[2] == "token=[REDACTED:GITHUB_TOKEN]"
    # steps_root and the Merkle root cover the rows.
    assert envelope["steps_root"] == compute_steps_root(steps)
    assert envelope["steps_merkle_root"] == compute_steps_merkle_root(steps)


@pytest.mark.asyncio
async def test_one_flipped_byte_in_a_connector_step_breaks_the_chain() -> None:
    envelope = await _seal(
        _connector_call("c1", "search_repos", preimage="a"),
        _connector_call("c2", "list_issues", preimage="b"),
    )
    tampered = copy.deepcopy(envelope)
    digest = tampered["steps"][1]["output_hash"]
    tampered["steps"][1]["output_hash"] = ("0" if digest[0] != "0" else "1") + digest[
        1:
    ]

    verdict = verify_run_attestation(tampered)
    assert not verdict.valid
    # The record is well-formed; the step chain is the broken link.
    assert verdict.checks == {
        "schema": True,
        "steps_root": False,
        "steps_merkle_root": False,
        "signature": False,
    }


@pytest.mark.asyncio
async def test_a_failed_platform_call_is_present_with_success_false() -> None:
    envelope = await _seal(
        _connector_call("c1", "search_repos", preimage="ok"),
        _connector_call(
            "c2",
            "list_issues",
            preimage="Error: connection refused",
            success=False,
            content="Error: connection refused",
        ),
    )
    assert verify_run_attestation(envelope).valid
    failed = envelope["steps"][1]
    assert failed["tool"] == f"{CONNECTION}#list_issues"
    assert failed["success"] is False
    assert failed["output_hash"] == _hash("Error: connection refused")


@pytest.mark.asyncio
async def test_a_retried_call_is_one_row_holding_the_last_attempt() -> None:
    envelope = await _seal(
        _connector_call("c1", "search_repos", preimage="Error: 503", success=False),
        _connector_call("c1", "search_repos", preimage="Error: 503", success=False),
        _connector_call("c2", "list_issues", preimage="issues"),
        _connector_call("c1", "search_repos", preimage="repos"),
    )
    assert verify_run_attestation(envelope).valid
    assert [s["call_id"] for s in envelope["steps"]] == ["c1", "c2"]
    first = envelope["steps"][0]
    assert first["success"] is True
    assert first["output_hash"] == _hash("repos")


@pytest.mark.asyncio
async def test_exhausted_retries_leave_one_failed_row() -> None:
    envelope = await _seal(
        _connector_call("c1", "search_repos", preimage="Error: 503", success=False),
        _connector_call("c1", "search_repos", preimage="Error: timeout", success=False),
    )
    (step,) = envelope["steps"]
    assert step["success"] is False
    assert step["output_hash"] == _hash("Error: timeout")


@pytest.mark.asyncio
async def test_without_a_preimage_the_display_content_is_hashed() -> None:
    record = _step_result("c1", content="shown text")
    envelope = await _seal(record)
    assert envelope["steps"][0]["output_hash"] == _hash("shown text")


def test_system_tools_and_off_charset_capabilities_keep_a_bare_name() -> None:
    assert step_tool("did:orcha:system:tools", "run_tests", "run_tests") == "run_tests"
    assert (
        step_tool(CONNECTION, "search_repos", "gh__x") == f"{CONNECTION}#search_repos"
    )
    assert step_tool(CONNECTION, "has#hash", "gh__x") == "gh__x"
    assert step_tool(CONNECTION, "bad\n", "gh__x") == "gh__x"
    assert step_tool(CONNECTION, "", "gh__x") == "gh__x"
    assert step_tool(CONNECTION, None, "gh__x") == "gh__x"
    assert step_tool("did:agent:legacy", "cap", "legacy_tool") == "legacy_tool"
