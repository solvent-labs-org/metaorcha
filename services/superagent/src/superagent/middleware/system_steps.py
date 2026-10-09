"""Emit RFC 0003 steps for system-tool calls (PRD FR-7, story 3.1).

External agent calls go through ExecutionMiddleware, which emits a
``StepResult``. System tools dispatch via ``SYSTEM_TOOL_REGISTRY`` and used to
bypass that seam, so a platform tool call never landed in ``steps[]``. This
helper is the small blast-radius option from PRD OQ-3: emit a step event, run
no payment or preflight on platform tools.

What a system step signs, and what it does not:

- the call's ``args_hash``, ``output_hash`` over the raw result's pre-image
  (AD-16) and ``success``, so ``steps_root`` covers it;
- the turn's criteria digest and run-level operands (``criteria_step_meta``),
  like every agent step, so a ``counts_match`` routine firing is judged on
  the same metadata whether or not it touched a platform tool;
- **no structural verdict**: no structural check runs on a platform tool, so
  the step is unchecked rather than "verified"; ``success`` is read from the
  tool's own result (``system_result_success``): a raised tool, an
  ``Error:`` result, or a platform tool's ``{"error": ...}`` /
  ``{"ok": false}`` reply is ``success: false``, without refusing the run;
- ``content`` is the display copy, capped like an agent step's card: the
  step fans out over Kafka with it, and a platform tool's full result (a
  memory read, a mailed address) must not leave the process that way
  (``step_events``); the hash still covers the full result;
- ``declared_acceptance`` for the declared step criteria, read from the raw
  result: ``exit_zero`` as on any step, ``citations_required`` ``n/a`` (a
  platform tool never carries citations), so a cited turn that also touched
  a checklist is judged on its agent steps (``system_step_declared_acceptance``).
"""

from __future__ import annotations

import logging
from typing import Any

from .observers import StepResult, emit_step_complete

logger = logging.getLogger(__name__)

SYSTEM_TOOL_AGENT_DID = "did:orcha:system:tools"
SYSTEM_TOOL_PROTOCOL = "SYSTEM"
# The normalizer's cut for an agent step's display copy (output_normalizer).
_CONTENT_CAP = 280


def system_result_success(raw_result: Any, content: str) -> bool:
    """Whether a platform tool's call succeeded, read from its own reply.

    Platform tools report failure two ways: by raising (the node turns that
    into ``Error: ...`` content) or by returning ``{"error": ...}`` /
    ``{"ok": false, ...}`` (checklist, artifacts, save_artifact). Both are
    ``False`` here; a tool's reply is never read as success just because it
    came back.
    """
    if content.startswith(("Error:", "Input error:", "Unsupported protocol:")):
        return False
    if isinstance(raw_result, dict):
        if raw_result.get("error"):
            return False
        if raw_result.get("ok") is False:
            return False
    return True


async def attest_system_tool_step(
    *,
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
    raw_result: Any,
    content: str,
    success: bool,
    latency_ms: int,
    state: dict[str, Any],
) -> None:
    """Record one system-tool invocation on the observer seam. Never raises."""
    try:
        from .step_output import step_output_preimage

        declared_meta: dict[str, Any] = {}
        criteria = state.get("_declared_criteria")
        if isinstance(criteria, dict) and criteria:
            from .criteria import (
                criteria_step_meta,
                step_criteria,
                system_step_declared_acceptance,
            )

            declared_meta = criteria_step_meta(criteria, state.get("routine_context"))
            per_step = step_criteria(criteria)
            if per_step:
                declared_meta["declared_acceptance"] = system_step_declared_acceptance(
                    per_step, raw_result
                )

        step = StepResult(
            call_id=call_id,
            agent_id=SYSTEM_TOOL_AGENT_DID,
            capability_id=tool_name,
            protocol=SYSTEM_TOOL_PROTOCOL,
            tool_name=tool_name,
            success=success,
            content=content[:_CONTENT_CAP],
            # Platform tools resolve no connection credentials (AD-14).
            output_preimage=step_output_preimage(raw_result, []),
            user_id=str(state.get("user_id") or ""),
            session_id=str(state.get("session_id") or ""),
            latency_ms=latency_ms,
            verdict=None,
            metadata={"goal": "", **declared_meta},
            args=dict(args),
        )
    except Exception:
        logger.exception("system step for %r not built", tool_name)
        return
    await emit_step_complete(step)
