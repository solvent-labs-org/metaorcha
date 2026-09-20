"""Emit RFC 0003 steps for system-tool calls (PRD FR-7).

External agent calls already go through ExecutionMiddleware, which emits
``StepResult``. System tools dispatch via ``SYSTEM_TOOL_REGISTRY`` and used
to bypass that seam, so a test-suite invocation never landed in ``steps[]``.
This helper is the small blast-radius option from PRD OQ-3: emit a step
event, do not run payment/preflight on platform tools.
"""

from __future__ import annotations

from typing import Any

from .observers import StepResult, emit_step_complete

SYSTEM_TOOL_AGENT_DID = "did:orcha:system:tools"
SYSTEM_TOOL_PROTOCOL = "SYSTEM"


async def attest_system_tool_step(
    *,
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
    content: str,
    success: bool,
    latency_ms: int,
    state: dict[str, Any],
) -> None:
    """Record one system-tool invocation on the observer seam. Never raises."""
    declared_meta: dict[str, Any] = {}
    criteria = state.get("_declared_criteria")
    if isinstance(criteria, dict) and criteria:
        from .criteria import criteria_digest, step_declared_acceptance

        declared_meta["criteria_digest"] = criteria_digest(criteria)
        declared_meta["declared_acceptance"] = step_declared_acceptance(
            criteria, content
        )

    await emit_step_complete(
        StepResult(
            call_id=call_id,
            agent_id=SYSTEM_TOOL_AGENT_DID,
            capability_id=tool_name,
            protocol=SYSTEM_TOOL_PROTOCOL,
            tool_name=tool_name,
            success=success,
            content=content,
            user_id=str(state.get("user_id") or ""),
            session_id=str(state.get("session_id") or ""),
            latency_ms=latency_ms,
            verdict={"verified": success, "reason": "ok" if success else content[:120]},
            metadata={"goal": "", **declared_meta},
            args=dict(args),
        )
    )
