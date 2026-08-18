"""execute_agent_calls_node — dispatches tool calls to middleware pipeline."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from internal_commons.interrupts.events import InterruptEvent
from internal_commons.interrupts.types import InterruptType
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from ..graph.state import ArtifactRef
from ..kya_policy import agent_allowed, system_tool_allowed
from ..middleware.manifest_cache import MANIFEST_CACHE
from ..middleware.oauth_grants import (
    capability_grant_key as _redis_cap_key,
)
from ..middleware.oauth_grants import (
    config_grant_key as _redis_cfg_key,
)
from ..middleware.oauth_grants import (
    store_grants_for_strategy,
)
from ..middleware.preflight import AuthInterruptRequired, PreFlightError
from ..persistence.transcript_store import TRANSCRIPT_TOOL_META_KEY
from ..pnd.candidate_compat import (
    cand_agent_id,
    cand_agent_name,
    cand_capabilities,
    cand_protocol_type,
    cap_capability_id,
)
from ..pricing.guard import PaymentInterrupt
from ..runtime.session_cancel import is_cancelled, session_id_from_config

logger = logging.getLogger(__name__)


def _tool_transcript_meta(
    *,
    agent_id: str,
    capability_id: str = "",
    protocol: str = "",
    internal_tool_name: str = "",
    invocation_args: dict[str, Any] | None = None,
    base_fee: str = "0",
    verified: bool | None = None,
    verdict_reason: str = "",
    total_cost_usd: str = "",
) -> dict[str, Any]:
    """Structured fields stored in SessionTranscriptEntry.tool_inputs for UI + hydration."""
    meta: dict[str, Any] = {
        "agent_id": agent_id,
        "capability_id": capability_id,
        "protocol": protocol,
        "internal_tool_name": internal_tool_name,
    }
    if invocation_args is not None:
        meta["invocation_args"] = invocation_args
    if base_fee and base_fee != "0":
        meta["base_fee"] = base_fee
    if verified is not None:
        meta["verified"] = verified
    if verdict_reason:
        meta["verdict_reason"] = verdict_reason
    if total_cost_usd and total_cost_usd != "0":
        meta["total_cost_usd"] = total_cost_usd
    return meta


def _agent_display_name(agent_id: str, pnd_candidates: list[Any]) -> str:
    """Human-readable agent label for transcript / ToolMessage.name."""
    for c in pnd_candidates:
        if cand_agent_id(c) == agent_id:
            n = (cand_agent_name(c) or "").strip()
            if n:
                return n
    if not agent_id:
        return "Agent"
    if agent_id == "_system":
        return "System"
    if agent_id == "_unresolved":
        return "Unresolved tool"
    tail = agent_id.rsplit(":", 1)[-1]
    return tail.replace("_", " ").strip() or agent_id


def _external_tool_display_name(
    *,
    agent_id: str,
    internal_tool_name: str,
    pnd_candidates: list[Any],
) -> str:
    if agent_id == "_system":
        return internal_tool_name or "system tool"
    if agent_id == "_unresolved":
        return internal_tool_name or "unknown tool"
    return _agent_display_name(agent_id, pnd_candidates)


def parse_agent_call(
    tool_name: str,
    pnd_candidates: list[Any],
) -> tuple[str, str, str] | None:
    """
    Resolve a sanitised tool name back to (agent_id, capability_id, protocol).

    Returns None for system tools (they don't need this resolution).
    """
    import re

    _INVALID = re.compile(r"[^a-zA-Z0-9_-]")

    if tool_name.startswith("delegate__"):
        safe_id = tool_name[len("delegate__") :]
        for c in pnd_candidates:
            agent_id = cand_agent_id(c)
            if _INVALID.sub("_", agent_id) == safe_id:
                return agent_id, "", cand_protocol_type(c) or "A2A"
        return None

    if "__" in tool_name:
        safe_agent, capability_id = tool_name.rsplit("__", 1)
        for c in pnd_candidates:
            agent_id = cand_agent_id(c)
            if _INVALID.sub("_", agent_id) != safe_agent:
                continue
            # Validate capability_id is actually registered for this agent
            registered = {cap_capability_id(cap) for cap in cand_capabilities(c)}
            if capability_id not in registered:
                logger.warning(
                    "LLM called hallucinated capability %r for agent %s — "
                    "registered capabilities: %s",
                    capability_id,
                    agent_id,
                    sorted(registered),
                )
                return None
            return agent_id, capability_id, cand_protocol_type(c) or "MCP"
        return None

    return None


def _bind_step_to_call(
    checklist: Any,
    tool_name: str,
    call_id: str,
    agent_id: str,
) -> None:
    """
    Stamp call_id, tool_name_resolved, and started_at on the matching checklist step.

    Matching strategy (first match wins):
        1. step.agent_id is set and contained in tool_name
        2. step is unbound (no call_id and no agent_id set yet) — greedy first-fit
    """
    if checklist is None:
        return
    now = datetime.now(UTC).isoformat()
    for step in getattr(checklist, "steps", []):
        if getattr(step, "status", "") not in ("pending", "in_progress"):
            continue
        step_agent = getattr(step, "agent_id", None)
        already_bound = getattr(step, "call_id", None) is not None
        if already_bound:
            continue
        if (step_agent and step_agent in tool_name) or not step_agent:
            step.call_id = call_id
            step.tool_name_resolved = tool_name
            step.agent_id = step_agent or agent_id
            step.status = "in_progress"
            step.started_at = now
            break


async def _emit_invocation(
    config: RunnableConfig,
    payload: dict[str, Any],
    pending_events: list[dict[str, Any]],
) -> None:
    """Emit a custom stream event; always queue a copy for values-mode drain (belt-and-suspenders)."""
    pending_events.append(dict(payload))
    try:
        await adispatch_custom_event("agent_invocation", payload, config=config)
    except Exception:
        logger.debug(
            "adispatch_custom_event failed for %s call_id=%s",
            payload.get("type"),
            payload.get("call_id"),
            exc_info=True,
        )


def _result_status(content: str) -> str:
    if content.startswith("Error:") or content.startswith("Input error:"):
        return "error"
    if content.startswith("Unsupported protocol:"):
        return "error"
    return "success"


def _canvas_manifest_id(ui_manifest: Any, call_id: str) -> str:
    """Resolve the manifest_id for a canvas_manifest event.

    An agent may supply a stable ``manifest.id`` so a dashboard it re-renders
    addresses the same canvas across runs, letting the client update in place
    rather than append a duplicate. Absent, blank, or non-string falls back to
    the per-call id, which is the previous behaviour.
    """
    if isinstance(ui_manifest, dict):
        supplied = ui_manifest.get("id")
        if isinstance(supplied, str) and supplied.strip():
            return supplied.strip()
    return f"canvas-{call_id}"


async def _reject_disallowed_tool_call(
    config: RunnableConfig,
    pending_events: list[dict[str, Any]],
    tool_messages: list[ToolMessage],
    result_store: dict[str, Any],
    *,
    tool_name: str,
    call_id: str,
    args: dict[str, Any],
    agent_id: str,
    reason: str,
) -> None:
    """Reject a tool call blocked by the KY-A allowlist (FR-10.1) — never executes."""
    err_content = f"Error: {reason}"
    await _emit_invocation(
        config,
        {
            "type": "invocation_start",
            "call_id": call_id,
            "tool_name": tool_name,
            "agent_id": agent_id,
            "inputs": args,
        },
        pending_events,
    )
    tool_messages.append(
        ToolMessage(
            content=err_content,
            tool_call_id=call_id,
            name=tool_name or "disallowed tool",
            additional_kwargs={
                TRANSCRIPT_TOOL_META_KEY: _tool_transcript_meta(
                    agent_id=agent_id,
                    internal_tool_name=tool_name,
                    invocation_args=args,
                )
            },
        )
    )
    result_store[call_id] = err_content
    await _emit_invocation(
        config,
        {
            "type": "invocation_result",
            "call_id": call_id,
            "tool_name": tool_name,
            "agent_id": agent_id,
            "status": "error",
            "content_preview": err_content[:300],
        },
        pending_events,
    )


async def _execute_with_retry(
    middleware: Any,
    *,
    agent_id: str,
    capability_id: str,
    protocol: str,
    tool_name: str,
    args: dict[str, Any],
    call_id: str,
    config: RunnableConfig,
    pending_events: list[dict[str, Any]],
    max_retries: int,
) -> dict[str, Any]:
    """Run ``middleware.execute()`` with a bounded retry on transient failures.

    The verifier retry-gate (v1.3): a step that raises a *transient* error
    (timeout / connection / 429 / 5xx — classified by the Phase-1 taxonomy in
    ``runner._classify_stream_error``) is re-run up to ``max_retries`` times
    before its result is committed. The transient exception is raised *before*
    payment settlement (pipeline step 6.5), so a retry never double-charges.

    Interrupts (auth / payment / graph) and non-retriable errors propagate
    unchanged to the caller's existing ``except`` handlers — this wrapper only
    intercepts retriable dispatch failures on the happy path.
    """
    # Local import avoids a module-level import cycle (runner ⇄ nodes).
    from ..graph.runner import _classify_stream_error

    attempt = 0
    while True:
        attempt += 1
        try:
            return await middleware.execute(
                agent_id=agent_id,
                capability_id=capability_id,
                protocol=protocol,
                tool_name=tool_name,
                args=args,
                call_id=call_id,
                config=config,
            )
        except (AuthInterruptRequired, PaymentInterrupt, GraphInterrupt):
            raise  # control-flow interrupts are never retried
        except Exception as exc:
            cls = _classify_stream_error(exc)
            if cls.get("retriable") and attempt <= max_retries:
                await _emit_invocation(
                    config,
                    {
                        "type": "invocation_retry",
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "agent_id": agent_id,
                        "attempt": attempt,
                        "max_attempts": max_retries + 1,
                        "reason": cls.get("category", "transient"),
                    },
                    pending_events,
                )
                logger.info(
                    "execute_agent_calls: retry %s (attempt %d/%d) — %s",
                    tool_name,
                    attempt,
                    max_retries + 1,
                    cls.get("category"),
                )
                continue
            raise


async def execute_agent_calls_node(
    state: dict[str, Any],
    config: RunnableConfig,
) -> dict[str, Any]:
    """
    Execute all tool calls from the last AI message sequentially.

    System tools are dispatched to the SystemToolRegistry.
    External agent calls go through the ExecutionMiddleware pipeline.
    """
    from ..middleware.pipeline import ExecutionMiddleware
    from ..system_tools.registry import SYSTEM_TOOL_REGISTRY

    messages = state.get("messages", [])
    if not messages:
        return {}

    last_msg = messages[-1]
    if not isinstance(last_msg, AIMessage):
        return {}

    tool_calls = getattr(last_msg, "tool_calls", []) or []
    if not tool_calls:
        return {}

    logger.info(
        "execute_agent_calls: dispatching %d tool call(s): [%s]",
        len(tool_calls),
        ", ".join(tc.get("name", "?") for tc in tool_calls),
    )

    sid = session_id_from_config(config)
    pnd_candidates = state.get("pnd_candidates", [])
    tool_messages: list[ToolMessage] = []
    result_store: dict[str, Any] = {}
    system_tool_called = False
    pending_events: list[dict[str, Any]] = []

    # Keys that system tools may mutate directly on the state dict
    _MUTABLE_STATE_KEYS = ("task_checklist", "captured_workflow")

    for idx, tc in enumerate(tool_calls):
        if sid and is_cancelled(sid):
            cancel_msg = "Execution stopped by user."
            for rem in tool_calls[idx:]:
                rid = str(rem.get("id", ""))
                rname = str(rem.get("name", "") or "cancelled")
                rargs = rem.get("args") if isinstance(rem.get("args"), dict) else {}
                tool_messages.append(
                    ToolMessage(
                        content=cancel_msg,
                        tool_call_id=rid,
                        name=rname,
                        additional_kwargs={
                            TRANSCRIPT_TOOL_META_KEY: _tool_transcript_meta(
                                agent_id="_system",
                                internal_tool_name=rname,
                                invocation_args=rargs,
                            )
                        },
                    )
                )
                await _emit_invocation(
                    config,
                    {
                        "type": "invocation_result",
                        "call_id": rid,
                        "tool_name": rname,
                        "agent_id": "_system",
                        "status": "error",
                        "content_preview": cancel_msg[:300],
                    },
                    pending_events,
                )
                result_store[rid] = cancel_msg
            logger.info(
                "execute_agent_calls: kill-switch — skipped %d remaining tool call(s)",
                len(tool_calls) - idx,
            )
            break

        _call_base_fee: str = "0"  # reset for each tool call
        tool_name: str = tc.get("name", "")
        call_id: str = tc.get("id", "")
        args: dict[str, Any] = (
            tc.get("args", {}) if isinstance(tc.get("args"), dict) else {}
        )

        # Try system tool first
        if SYSTEM_TOOL_REGISTRY.has(tool_name):
            # KY-A mode (FR-10.1): reject system tools outside the allowlist.
            if not system_tool_allowed(tool_name):
                logger.warning(
                    "execute_agent_calls: KY-A allowlist rejected system tool %r",
                    tool_name,
                )
                await _reject_disallowed_tool_call(
                    config,
                    pending_events,
                    tool_messages,
                    result_store,
                    tool_name=tool_name,
                    call_id=call_id,
                    args=args,
                    agent_id="_system",
                    reason=(
                        f"system tool {tool_name!r} is not on the KY-A "
                        "supervisor allowlist (FR-10.1)"
                    ),
                )
                continue
            system_tool_called = True
            await _emit_invocation(
                config,
                {
                    "type": "invocation_start",
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "agent_id": "_system",
                    "inputs": args,
                },
                pending_events,
            )
            try:
                result = await SYSTEM_TOOL_REGISTRY.call(tool_name, args, state)
                if (
                    tool_name == "save_artifact"
                    and isinstance(result, dict)
                    and result.get("ok")
                ):
                    aid = result.get("artifact_id")
                    if aid:
                        ref = ArtifactRef(
                            artifact_id=str(aid),
                            filename=str(result.get("filename", "")),
                            mime_type=str(
                                result.get("mime_type", "application/octet-stream")
                            ),
                            size_bytes=int(result.get("size_bytes") or 0),
                            s3_bucket=str(result.get("s3_bucket", "")),
                            s3_key=str(result.get("s3_key", "")),
                        )
                        existing_artifacts = dict(state.get("artifacts") or {})
                        existing_artifacts[ref.artifact_id] = ref
                        state["artifacts"] = existing_artifacts
                        await _emit_invocation(
                            config,
                            {
                                "type": "artifact_created",
                                "artifact_id": ref.artifact_id,
                                "filename": ref.filename,
                                "mime_type": ref.mime_type,
                                "size_bytes": ref.size_bytes,
                                "source": "AGENT_OUTPUT",
                            },
                            pending_events,
                        )
                content = json.dumps(result) if not isinstance(result, str) else result
            except GraphInterrupt:
                # System tools like propose_enforcement suspend the graph via
                # langgraph interrupt() — it must propagate, never be swallowed
                # into an error ToolMessage.
                raise
            except Exception as exc:
                logger.exception("System tool %r failed", tool_name)
                content = f"Error: {exc}"
            tool_messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call_id,
                    name=tool_name,
                    additional_kwargs={
                        TRANSCRIPT_TOOL_META_KEY: _tool_transcript_meta(
                            agent_id="_system",
                            internal_tool_name=tool_name,
                            invocation_args=args,
                        )
                    },
                )
            )
            result_store[call_id] = content
            await _emit_invocation(
                config,
                {
                    "type": "invocation_result",
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "agent_id": "_system",
                    "status": _result_status(content),
                    "content_preview": content[:300],
                },
                pending_events,
            )
            continue

        # External agent call — resolve via pnd_candidates
        resolved = parse_agent_call(tool_name, pnd_candidates)
        if resolved is None:
            logger.warning(
                "Could not resolve tool call %r — no matching agent", tool_name
            )
            err_content = f"Error: unknown tool {tool_name!r}"
            await _emit_invocation(
                config,
                {
                    "type": "invocation_start",
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "agent_id": "_unresolved",
                    "inputs": args,
                },
                pending_events,
            )
            tool_messages.append(
                ToolMessage(
                    content=err_content,
                    tool_call_id=call_id,
                    name=tool_name or "unknown tool",
                    additional_kwargs={
                        TRANSCRIPT_TOOL_META_KEY: _tool_transcript_meta(
                            agent_id="_unresolved",
                            internal_tool_name=tool_name,
                            invocation_args=args,
                        )
                    },
                )
            )
            result_store[call_id] = err_content
            await _emit_invocation(
                config,
                {
                    "type": "invocation_result",
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "agent_id": "_unresolved",
                    "status": "error",
                    "content_preview": err_content[:300],
                },
                pending_events,
            )
            continue

        agent_id, capability_id, protocol = resolved

        # KY-A mode (FR-10.1): reject external agents outside the allowlisted DIDs.
        if not agent_allowed(agent_id):
            logger.warning(
                "execute_agent_calls: KY-A allowlist rejected agent %s (tool %r)",
                agent_id,
                tool_name,
            )
            await _reject_disallowed_tool_call(
                config,
                pending_events,
                tool_messages,
                result_store,
                tool_name=tool_name,
                call_id=call_id,
                args=args,
                agent_id=agent_id,
                reason=(
                    f"agent {agent_id!r} is not on the KY-A supervisor "
                    "allowlist (FR-10.1)"
                ),
            )
            continue

        display_name = _external_tool_display_name(
            agent_id=agent_id,
            internal_tool_name=tool_name,
            pnd_candidates=pnd_candidates,
        )

        # Bind checklist step to this call before dispatch
        _bind_step_to_call(state.get("task_checklist"), tool_name, call_id, agent_id)

        await _emit_invocation(
            config,
            {
                "type": "invocation_start",
                "call_id": call_id,
                "tool_name": tool_name,
                "agent_id": agent_id,
                "capability_id": capability_id,
                "protocol": protocol,
                "inputs": args,
            },
            pending_events,
        )

        _call_base_fee = "0"
        _call_total_cost = "0"
        _verified = True
        _verdict_reason = "ok"
        logger.info(
            "execute_agent_calls: → %s | agent=%s capability=%s protocol=%s args=%s",
            tool_name,
            agent_id,
            capability_id,
            protocol,
            json.dumps(args, default=str)[:600],
        )
        try:
            from ..config import settings as _sa_settings

            _verify_max_retries = max(
                0, int(getattr(_sa_settings, "verify_max_retries", 2))
            )
            middleware = ExecutionMiddleware(state=state)
            result = await _execute_with_retry(
                middleware,
                agent_id=agent_id,
                capability_id=capability_id,
                protocol=protocol,
                tool_name=tool_name,
                args=args,
                call_id=call_id,
                config=config,
                pending_events=pending_events,
                max_retries=_verify_max_retries,
            )
            content = result.get("content", "")
            _max_out = int(getattr(_sa_settings, "tool_output_max_chars", 0) or 0)
            if _max_out > 0 and len(content) > _max_out:
                content = (
                    content[:_max_out]
                    + f"\n… [truncated: {len(content) - _max_out} chars omitted]"
                )
            _call_base_fee = result.get("base_fee", "0")
            _call_total_cost = result.get("total_cost_usd", _call_base_fee)
            _verified = result.get("verified", True)
            _verdict_reason = result.get("verdict_reason", "ok")
            logger.info(
                "execute_agent_calls: ← %s | result_len=%d result_preview=%s",
                tool_name,
                len(content),
                content[:800],
            )
            # Handle agent-produced artifact (e.g. doc-convert file output)
            produced_artifact = result.get("artifact")
            if produced_artifact is not None:
                artifact_id = getattr(produced_artifact, "artifact_id", None)
                if artifact_id:
                    existing_artifacts = dict(state.get("artifacts") or {})
                    existing_artifacts[artifact_id] = produced_artifact
                    state["artifacts"] = existing_artifacts
                    await _emit_invocation(
                        config,
                        {
                            "type": "artifact_created",
                            "artifact_id": artifact_id,
                            "filename": getattr(produced_artifact, "filename", ""),
                            "mime_type": getattr(produced_artifact, "mime_type", ""),
                            "size_bytes": getattr(produced_artifact, "size_bytes", 0),
                            "source": "AGENT_OUTPUT",
                        },
                        pending_events,
                    )
            # Handle agent-produced UIManifest (CanvasKit dashboard)
            ui_manifest = result.get("ui_manifest")
            if ui_manifest is not None:
                await _emit_invocation(
                    config,
                    {
                        "type": "canvas_manifest",
                        "manifest_id": _canvas_manifest_id(ui_manifest, call_id),
                        "manifest": ui_manifest,
                        "call_id": call_id,
                    },
                    pending_events,
                )
        except AuthInterruptRequired as exc:
            # Node-level interrupt: suspends the graph until the user provides credentials.
            # On the first call interrupt() raises GraphInterrupt; on node re-execution
            # after resume it returns the stored resume value.
            logger.info(
                "execute_agent_calls: auth interrupt required for agent=%s — suspending",
                agent_id,
            )
            resume_value: dict[str, Any] = interrupt(exc.event.model_dump())

            if (
                exc.event.interrupt_type == InterruptType.AUTH_FORM_SUBMISSION
                and (resume_value.get("status") or "").lower() == "complete"
            ):
                vault_key = str(resume_value.get("vault_key") or "").strip()
                credential_value = str(
                    resume_value.get("credential_value") or ""
                ).strip()
                if vault_key and credential_value:
                    from ..vault.client import VaultClient

                    try:
                        await VaultClient().save_user_secret(
                            str(state.get("user_id") or ""),
                            vault_key,
                            credential_value,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist AUTH_FORM_SUBMISSION credential for user=%s key=%s",
                            state.get("user_id", ""),
                            vault_key,
                        )

            # Node re-executing after resume — retry preflight (credentials now in vault)
            if resume_value.get("status") == "denied":
                content = f"Error: Authorization denied by user for agent {agent_id!r}"
            else:
                event_meta = exc.event.metadata or {}
                provider_name = str(event_meta.get("provider_name") or "")
                scopes: list[str] = event_meta.get("scopes") or []
                user_id: str = str(state.get("user_id") or "")

                # Write Redis grants covering ALL capabilities that share the same
                # auth strategy (fan-out), then fall back to in-memory for the
                # current preflight check within this node execution.
                try:
                    manifest = await MANIFEST_CACHE.get_manifest(agent_id)
                    await store_grants_for_strategy(
                        user_id=user_id,
                        agent_id=agent_id,
                        provider_name=provider_name,
                        scopes=scopes,
                        manifest=manifest,
                    )
                except Exception:
                    logger.warning(
                        "oauth_grant_fanout_failed agent=%s — falling back to in-memory grant",
                        agent_id,
                        exc_info=True,
                    )

                # Keep an in-memory grant in session state so the preflight check
                # later in this same node execution (middleware2) passes immediately
                # without waiting for a Redis round-trip.
                oauth_grants: dict[str, bool] = state.get("_agent_oauth_grants", {})
                capability_key = _redis_cap_key(agent_id, capability_id)
                oauth_grants[capability_key] = True
                if scopes:
                    oauth_grants[_redis_cfg_key(agent_id, provider_name, scopes)] = True
                state["_agent_oauth_grants"] = oauth_grants

                logger.info(
                    "oauth_grant_stored agent=%s capability=%s user=%s provider=%s",
                    agent_id,
                    capability_id or "*",
                    user_id,
                    provider_name,
                )
                try:
                    middleware2 = ExecutionMiddleware(state=state)
                    result2 = await middleware2.execute(
                        agent_id=agent_id,
                        capability_id=capability_id,
                        protocol=protocol,
                        tool_name=tool_name,
                        args=args,
                        call_id=call_id,
                        config=config,
                    )
                    content = result2.get("content", "")
                    _call_base_fee = result2.get("base_fee", "0")
                    _call_total_cost = result2.get("total_cost_usd", _call_base_fee)
                except AuthInterruptRequired:
                    content = f"Error: Auth still unresolved for agent {agent_id!r} after user interaction"
                except PreFlightError as pfe:
                    content = f"Error: {pfe}"
                except Exception as retry_exc:
                    logger.exception("Agent call %r failed on resume retry", tool_name)
                    content = f"Error: {retry_exc}"
        except PaymentInterrupt as exc:
            logger.info(
                "execute_agent_calls: payment interrupt for agent=%s reason=%s",
                agent_id,
                exc.reason,
            )
            import secrets

            session_id: str = str(state.get("session_id") or "")
            nonce = secrets.token_hex(4)
            interrupt_id = f"INSUFFICIENT_CREDITS__{agent_id}__{capability_id}__{nonce}"
            payment_event = InterruptEvent(
                interrupt_type=InterruptType.INSUFFICIENT_CREDITS,
                interrupt_id=interrupt_id,
                agent_id=agent_id,
                session_id=session_id,
                message=f"Insufficient credits to invoke {display_name}",
                metadata={
                    "reason": exc.reason,
                    "amount_owed": str(exc.amount),
                    "agent_display_name": display_name,
                },
            )
            interrupt(payment_event.model_dump())
            content = f"Error: Insufficient credits — {exc.reason}"
        except GraphInterrupt:
            # Safety net: let any other LangGraph-internal interrupt propagate cleanly
            raise
        except PreFlightError as exc:
            logger.error("PreFlight hard failure for %r: %s", tool_name, exc)
            content = f"Error: {exc}"
        except Exception as exc:
            logger.exception("Agent call %r failed", tool_name)
            content = f"Error: {exc}"

        # v1.3 verdict normalization — any Error content is unverified, no matter
        # which path produced it (returned string, raised+exhausted retry,
        # auth-denied, preflight). Previously a raised failure left the
        # initialized _verified=True, mislabelling failed steps as ✓ Verified.
        # Success keeps the middleware's structural verdict from pipeline step 5.5.
        if content.startswith(("Error:", "Input error:", "Unsupported protocol:")):
            _verified = False
            if not _verdict_reason or _verdict_reason == "ok":
                _verdict_reason = content[:120]

        cost_payload: dict[str, Any] = {}
        if _call_total_cost and _call_total_cost != "0":
            cost_payload["total_cost_usd"] = _call_total_cost
            cost_payload["base_fee"] = _call_base_fee

        await _emit_invocation(
            config,
            {
                "type": "invocation_result",
                "call_id": call_id,
                "tool_name": tool_name,
                "agent_id": agent_id,
                "status": _result_status(content),
                "content_preview": content[:300],
                "verified": _verified,
                "verdict_reason": _verdict_reason,
                **cost_payload,
            },
            pending_events,
        )

        transcript_meta = _tool_transcript_meta(
            agent_id=agent_id,
            capability_id=capability_id,
            protocol=protocol,
            internal_tool_name=tool_name,
            invocation_args=args,
            base_fee=_call_base_fee,
            verified=_verified,
            verdict_reason=_verdict_reason,
            total_cost_usd=_call_total_cost,
        )

        tool_messages.append(
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=display_name,
                additional_kwargs={TRANSCRIPT_TOOL_META_KEY: transcript_meta},
            )
        )
        result_store[call_id] = content

    updates: dict[str, Any] = {
        "messages": tool_messages,
        "agent_call_result_store": result_store,
        "_pending_events": pending_events,
        # Always flush artifacts — agent calls may have added new artifact refs
        "artifacts": dict(state.get("artifacts") or {}),
    }
    if system_tool_called:
        # Flush any state mutations (direct dict writes or in-place object mutations)
        # made by system tool handlers back into LangGraph state.
        for key in _MUTABLE_STATE_KEYS:
            updates[key] = state.get(key)
    # Persist in-memory OAuth grants so they survive across node re-executions
    # and don't trigger repeated auth prompts for the same session.
    if state.get("_agent_oauth_grants"):
        updates["_agent_oauth_grants"] = state["_agent_oauth_grants"]
    return updates
