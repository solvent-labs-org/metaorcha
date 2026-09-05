"""Simulated run: scripted steps through the REAL RFC 0003 builder and signer.

No crypto is reimplemented here — building, chaining, and signing all happen
in validator.run_envelope / emerge_node.envelope product code. Only the step
payloads and the demo key are playground-local.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from emerge_node.envelope import canonical_json_bytes, generate_keypair
from validator.run_envelope import (
    build_run_envelope,
    compute_steps_root,
    sha256_hex,
    sign_run_envelope,
)

from .state import run_store

SIM_SIGNER_DID = "did:orcha:system:playground-demo"
SIM_AGENT_DID = "did:orcha:agent:demo-research-agent"
POLICY_VERSION = "run-attestation/1.0"

# Demo keypair — generated once per process, clearly labelled by DID.
_SIM_PRIVATE_KEY, _SIM_PUBLIC_KEY_B64 = generate_keypair()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scripted_steps(prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "call_id": "sim-call-1",
            "tool": "search_docs",
            "args": {"query": prompt},
            "output": {"hits": ["docs/spec/rfcs/0003-run-attestation-envelope.md"]},
            "success": True,
            "latency_ms": 132,
        },
        {
            "call_id": "sim-call-2",
            "tool": "fetch_page",
            "args": {"path": "docs/spec/rfcs/0003-run-attestation-envelope.md"},
            "output": {
                "bytes": 14812,
                "sections": ["envelope", "hash chain", "signature"],
            },
            "success": True,
            "latency_ms": 214,
        },
        {
            "call_id": "sim-call-3",
            "tool": "summarize",
            "args": {"style": "engineering-brief"},
            "output": {
                "summary": "RFC 0003 defines a signed per-run attestation envelope."
            },
            "success": True,
            "latency_ms": 481,
            "cdv_bp": 910,
        },
    ]


def build_sim_envelope(prompt: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Build and sign a sim envelope entirely with product code."""
    started = _now()
    unsigned = build_run_envelope(
        run_id=run_id or f"sim-{started}",
        agent_dids=[SIM_AGENT_DID],
        charter_hash=sha256_hex(b"attestation playground demo charter v1"),
        policy_version=POLICY_VERSION,
        steps=_scripted_steps(prompt),
        verdicts=[
            {"check": "schema", "result": "pass"},
            {"check": "policy", "result": "pass"},
        ],
        started_at=started,
        finished_at=_now(),
        signer_did=SIM_SIGNER_DID,
        public_key_b64=_SIM_PUBLIC_KEY_B64,
    )
    return sign_run_envelope(unsigned, private_key=_SIM_PRIVATE_KEY)


def chain_events(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """One SSE event per canonical step, with its true chain prefix hash."""
    steps = envelope["steps"]
    events = []
    for i, step in enumerate(steps):
        events.append(
            {
                "type": "step",
                "step_index": i,
                "tool": step["tool"],
                "success": step["success"],
                "latency_ms": step["latency_ms"],
                "step_hash": sha256_hex(canonical_json_bytes(step)),
                "chain_hash": compute_steps_root(steps[: i + 1]),
            }
        )
    return events


async def run_sim(run_id: str, prompt: str, *, step_delay: float = 0.6) -> None:
    """Stream a scripted run into the store, then publish its envelope."""
    try:
        envelope = build_sim_envelope(prompt, run_id=run_id)
        for event in chain_events(envelope):
            run_store.emit(run_id, event)
            await asyncio.sleep(step_delay)
        run_store.set_envelope(run_id, envelope)
        run_store.emit(run_id, {"type": "envelope_ready", "run_id": run_id})
    except Exception as exc:  # demo must fail visibly, never hang
        run_store.set_failed(run_id, str(exc))
        run_store.emit(run_id, {"type": "run_failed", "error": str(exc)})
