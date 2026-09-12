"""SessionRunner — thin wrapper around the compiled LangGraph graph."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from internal_commons.interrupts.events import InterruptEvent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from ..pnd.candidate_compat import (
    cand_agent_id,
    cand_agent_name,
    cand_protocol_type,
)
from ..runtime.session_cancel import (
    register_run,
    signal_cancel,
    unregister_run,
)
from .state import default_state

logger = logging.getLogger(__name__)


def _artifact_attachments_metadata(
    initial_artifacts: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """JSON-safe list for USER transcript rows (tool_inputs.artifact_attachments)."""
    if not initial_artifacts:
        return None
    out: list[dict[str, Any]] = []
    for key, ref in initial_artifacts.items():
        if not isinstance(ref, dict):
            continue
        aid = str(ref.get("artifact_id") or key)
        fn = str(ref.get("filename") or "")
        mt = str(ref.get("mime_type") or "")
        try:
            sz = int(ref.get("size_bytes") or 0)
        except (TypeError, ValueError):
            sz = 0
        out.append(
            {
                "artifact_id": aid,
                "filename": fn,
                "mime_type": mt,
                "size_bytes": sz,
            }
        )
    return out or None


def _human_message(
    user_message: str, initial_artifacts: dict[str, Any] | None
) -> HumanMessage:
    """User turn message; optional artifact metadata for transcript + UI hydration."""
    att = _artifact_attachments_metadata(initial_artifacts)
    if not att:
        return HumanMessage(content=user_message)
    return HumanMessage(
        content=user_message,
        additional_kwargs={"artifact_attachments": att},
    )


def _cleanup_session_tmp(session_id: str) -> None:
    """Remove /tmp/{session_id}/ created by filesystem / MCP tool calls."""
    tmp_path = Path(f"/tmp/{session_id}")  # noqa: S108
    if tmp_path.exists():
        try:
            shutil.rmtree(tmp_path)
            logger.debug("Cleaned up session tmp dir %s", tmp_path)
        except Exception:
            logger.warning("Failed to clean up session tmp dir %s", tmp_path)


def _f(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute access that works for both dataclasses and plain dicts.

    LangGraph's checkpointer serialises dataclasses (ChecklistStep,
    TaskChecklist, …) to plain dicts when it persists state.  When the
    snapshot is loaded back the fields are therefore dict keys, not object
    attributes.  This helper handles both forms transparently.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


_STREAM_MODES: list[str] = ["values", "custom", "messages"]

_DEFAULT_RECURSION_LIMIT = 100


def _merge_graph_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = {**config}
    merged.setdefault("recursion_limit", _DEFAULT_RECURSION_LIMIT)
    return merged


def _invocation_dedupe_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    t = payload.get("type")
    cid = payload.get("call_id")
    if t == "invocation_progress":
        return (t, cid, payload.get("message", ""))
    if t == "invocation_retry":
        # Each retry attempt for the same call is a distinct event.
        return (t, cid, payload.get("attempt"))
    return (t, cid)


def _unpack_stream_item(item: Any) -> tuple[str, Any]:
    """
    LangGraph 1.1+ multi-mode astream yields either:
    - v1: (mode: str, chunk: Any)
    - v2 StreamPart-like dict: {"type": mode, "data": chunk, ...}
    """
    if isinstance(item, tuple) and len(item) >= 2:
        return str(item[0]), item[1]
    if isinstance(item, dict) and "type" in item:
        return str(item["type"]), item.get("data", item)
    return "values", item


def _message_chunk_text_delta(msg_chunk: AIMessageChunk) -> str:
    """Extract incremental text from an AIMessageChunk (string or content blocks)."""
    content = getattr(msg_chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


_AGENT_INVOCATION_TYPES = frozenset(
    (
        "invocation_start",
        "invocation_progress",
        "invocation_retry",
        "invocation_result",
        "canvas_manifest",
    )
)


def _parse_custom_agent_invocation(chunk: Any) -> dict[str, Any] | None:
    """Turn a 'custom' stream chunk into a SuperAgent SSE dict, if applicable."""
    if not isinstance(chunk, dict):
        return None
    if chunk.get("name") == "agent_invocation":
        data = chunk.get("data")
        if isinstance(data, dict):
            t = data.get("type")
            if t in _AGENT_INVOCATION_TYPES:
                return data
    t = chunk.get("type")
    if t in _AGENT_INVOCATION_TYPES:
        return chunk
    return None


def _merge_messages_for_transcript(
    from_checkpoint: list[Any],
    from_values_stream: list[Any] | None,
) -> list[Any]:
    """
    Prefer the values-stream list when it is at least as long as the checkpoint.

    After ``astream``, ``aget_state`` can briefly see a stale checkpoint (e.g. only
    the new ``HumanMessage``). Values-mode chunks carry the full merged state; when
    lengths tie, the stream snapshot is usually fresher than the checkpoint read.
    """
    a = from_checkpoint if isinstance(from_checkpoint, list) else []
    b = from_values_stream if isinstance(from_values_stream, list) else []
    if b and len(b) >= len(a):
        return list(b)
    return list(a)


# Sentinel for "the checkpoint read itself failed" — distinct from "no pending
# interrupt" (None). Callers must NOT treat a read error as a run boundary:
# the graph may actually be suspended, and sealing it as complete would attest
# a run that never finished (fail-open checkpoint read).
_CHECKPOINT_READ_FAILED: Any = object()


async def _pending_interrupt_event(
    graph: Any, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Read the checkpoint for a pending interrupt and shape it as an SSE event.

    With this LangGraph version, node-level ``interrupt()`` suspends the graph
    and records the payload in ``snapshot.tasks[*].interrupts``; it is not
    raised out of ``astream``. Emitting it here keeps SSE consumers (frontend,
    gate scripts) in sync with the checkpoint-driven ``get_status`` path.

    Returns ``_CHECKPOINT_READ_FAILED`` when the ``aget_state`` call raises, so
    callers can skip run-boundary dispatch instead of failing open.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.exception("pending_interrupt: aget_state failed")
        return _CHECKPOINT_READ_FAILED
    if not snapshot or not getattr(snapshot, "tasks", None):
        return None
    for task in snapshot.tasks:
        for intr in getattr(task, "interrupts", None) or []:
            val = getattr(intr, "value", None)
            if isinstance(val, dict):
                try:
                    return InterruptEvent.model_validate(val).model_dump(mode="json")
                except Exception:
                    logger.exception("pending_interrupt: invalid payload %r", val)
                    return val
    return None


async def _yield_multistream_events(
    graph: Any,
    stream_input: Any,
    config: dict[str, Any],
    *,
    values_messages_sink: dict[str, Any] | None = None,
):
    """Drive graph.astream with multiple modes and yield SuperAgent-shaped dicts."""
    merged_config = _merge_graph_config(config)
    seen_invocation: set[tuple[Any, ...]] = set()
    streamed_delta = False

    try:
        async for raw in graph.astream(
            stream_input,
            merged_config,
            stream_mode=_STREAM_MODES,
        ):
            try:
                mode, chunk = _unpack_stream_item(raw)
            except Exception:
                logger.exception("multistream: failed to unpack item %r", raw)
                continue

            if mode == "values":
                if values_messages_sink is not None and isinstance(chunk, dict):
                    vm = chunk.get("messages")
                    if vm is not None:
                        values_messages_sink["messages"] = vm
                try:
                    for event in _extract_events(
                        chunk,
                        skip_aimessage_token=streamed_delta,
                        seen_invocation=seen_invocation,
                    ):
                        yield event
                except Exception:
                    logger.exception(
                        "run_turn: failed to extract events from values chunk"
                    )
                streamed_delta = False
            elif mode == "messages":
                msg_chunk = (
                    chunk[0] if isinstance(chunk, (list, tuple)) and chunk else chunk
                )
                if isinstance(msg_chunk, AIMessageChunk):
                    delta = _message_chunk_text_delta(msg_chunk)
                    if delta:
                        streamed_delta = True
                        yield {"type": "token", "content": delta}
            elif mode == "custom":
                inv = _parse_custom_agent_invocation(chunk)
                if inv:
                    key = _invocation_dedupe_key(inv)
                    if key not in seen_invocation:
                        seen_invocation.add(key)
                        yield inv
    except GraphInterrupt as gi:
        # LangGraph suspends by raising GraphInterrupt((Interrupt(value=dict, ...), )).
        # gi.args[0] is a tuple of Interrupt objects — the actual payload is at [0].value.
        interrupt_objs = gi.args[0] if gi.args else ()
        first = (
            interrupt_objs[0]
            if isinstance(interrupt_objs, (tuple, list)) and interrupt_objs
            else None
        )
        raw_payload = getattr(first, "value", None) or {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        try:
            event = InterruptEvent.model_validate(raw_payload)
            yield event.model_dump()
        except Exception:
            logger.exception(
                "multistream: failed to reconstruct InterruptEvent from GraphInterrupt args=%r",
                gi.args,
            )
            yield {
                "type": "interrupt",
                "interrupt_type": raw_payload.get("interrupt_type", "unknown"),
                "interrupt_id": raw_payload.get("interrupt_id", ""),
                "agent_id": raw_payload.get("agent_id", ""),
                "session_id": raw_payload.get("session_id", ""),
                "message": raw_payload.get("message", "Authorization required"),
                "metadata": raw_payload.get("metadata", {}),
                "resumable": True,
            }


def _checkpoint_has_messages(snapshot: Any) -> bool:
    if snapshot is None or not getattr(snapshot, "values", None):
        return False
    vals = snapshot.values
    msgs = (
        vals.get("messages")
        if isinstance(vals, dict)
        else getattr(vals, "messages", None)
    )
    return bool(msgs)


class SessionRunner:
    """Manages per-session graph execution and HITL resume."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self._active_stream_tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_stream_lock = asyncio.Lock()

    async def _set_active_stream_task(
        self, session_id: str, task: asyncio.Task[Any] | None
    ) -> None:
        if task is None:
            return
        async with self._active_stream_lock:
            self._active_stream_tasks[session_id] = task

    async def _clear_active_stream_task(
        self, session_id: str, task: asyncio.Task[Any] | None
    ) -> None:
        if task is None:
            return
        async with self._active_stream_lock:
            current = self._active_stream_tasks.get(session_id)
            if current is task:
                self._active_stream_tasks.pop(session_id, None)

    async def stop_session_execution(self, session_id: str) -> dict[str, Any]:
        """Cancel the currently running stream task for a session, if any."""
        signal_cancel(session_id)
        async with self._active_stream_lock:
            task = self._active_stream_tasks.get(session_id)
            if task is None or task.done():
                self._active_stream_tasks.pop(session_id, None)
                return {"ok": False, "status": "not_running"}
            task.cancel()
        return {"ok": True, "status": "stopping"}

    async def _persist_transcript(
        self,
        session_id: str,
        session_credentials: dict[str, dict[str, str]] | None,
        *,
        stream_messages: list[Any] | None = None,
    ) -> None:
        from ..persistence.transcript_store import (
            get_session_meta,
            persist_new_messages,
            upsert_conversation_session,
        )

        cfg = _merge_graph_config(
            {
                "configurable": {
                    "thread_id": session_id,
                    "session_credentials": session_credentials or {},
                }
            }
        )
        try:
            snap = await self._graph.aget_state(cfg)
            vals = snap.values if snap else None
            msgs_snap = (vals.get("messages") if isinstance(vals, dict) else None) or []
            msgs = _merge_messages_for_transcript(msgs_snap, stream_messages)
            if stream_messages is not None and isinstance(stream_messages, list):
                if len(msgs_snap) != len(msgs) or (
                    len(msgs_snap) < len(stream_messages)
                ):
                    logger.info(
                        (
                            "transcript: merged messages for persist session=%s "
                            "aget_state=%d values_stream=%d chosen=%d"
                        ),
                        session_id,
                        len(msgs_snap),
                        len(stream_messages),
                        len(msgs),
                    )
            else:
                logger.info(
                    "transcript: persist source session=%s aget_state=%d values_stream=None chosen=%d",
                    session_id,
                    len(msgs_snap),
                    len(msgs),
                )
            meta = await get_session_meta(session_id)
            if meta is None:
                uid = vals.get("user_id") if isinstance(vals, dict) else None
                if uid:
                    try:
                        await upsert_conversation_session(session_id, str(uid), None)
                    except Exception:
                        logger.exception(
                            "transcript: ConversationSession upsert fallback failed session=%s",
                            session_id,
                        )
                        return
                    meta = await get_session_meta(session_id)
                if meta is None:
                    logger.warning(
                        "transcript persist skipped: no ConversationSession row session=%s",
                        session_id,
                    )
                    return
            prev, _ = meta
            _ = await persist_new_messages(session_id, list(msgs), prev)
        except Exception:
            logger.exception("transcript persist failed session=%s", session_id)

    async def run_turn(
        self,
        session_id: str,
        user_id: str,
        user_message: str,
        session_credentials: dict[str, dict[str, str]] | None = None,
        thread_config: dict[str, Any] | None = None,
        initial_artifacts: dict[str, Any] | None = None,
        lead_gen_options: dict[str, Any] | None = None,
        email_campaign_context: dict[str, Any] | None = None,
        model: str | None = None,
        custom_instructions: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:  # noqa: UP006
        """
        Execute one turn of the ReAct loop, yielding streaming events.

        Yields dicts of shape:
            {"type": "token",              "content": str}
            {"type": "checklist_snapshot", "checklist_id": str, "goal": str, "steps": [...]}
            {"type": "agents_discovered",  "agents": [...]}
            {"type": "token_usage",        "estimated_token_count": int}
            {"type": "artifact_created",   "artifact_id": str, ...}
            {"type": "invocation_start",   "tool_name": str, "call_id": str, ...}
            {"type": "invocation_progress", "call_id": str, "status": str, "message": str}
            {"type": "invocation_result",  "call_id": str, "content_preview": str, ...}
            {"type": "interrupt",          "interrupt_id": str, "interrupt_type": str, ...}
            {"type": "done",               "session_id": str}
        """
        config = _merge_graph_config(
            thread_config
            or {
                "configurable": {
                    "thread_id": session_id,
                    "session_credentials": session_credentials or {},
                }
            }
        )

        snapshot = await self._graph.aget_state(config)
        if _checkpoint_has_messages(snapshot):
            snap_vals = snapshot.values or {}
            state_update: dict[str, Any] = {
                "messages": [_human_message(user_message, initial_artifacts)]
            }
            # Merge any new artifact refs into existing state
            if initial_artifacts:
                existing = dict(snap_vals.get("artifacts") or {})
                existing.update(initial_artifacts)
                state_update["artifacts"] = existing
            if lead_gen_options:
                prev_lg = dict(snap_vals.get("lead_gen_options") or {})
                prev_lg.update(lead_gen_options)
                state_update["lead_gen_options"] = prev_lg
            if email_campaign_context:
                prev_ec = dict(snap_vals.get("email_campaign_context") or {})
                prev_ec.update(email_campaign_context)
                state_update["email_campaign_context"] = prev_ec
        else:
            from ..persistence.transcript_store import (
                load_transcript_rows,
                rows_to_langchain,
            )

            rows = await load_transcript_rows(session_id)
            if rows:
                hist = rows_to_langchain(rows)
                state_update = default_state(session_id, user_id)
                state_update["messages"] = hist + [
                    _human_message(user_message, initial_artifacts)
                ]
            else:
                state_update = default_state(session_id, user_id)
                state_update["messages"] = [
                    _human_message(user_message, initial_artifacts)
                ]

            if initial_artifacts:
                state_update["artifacts"] = initial_artifacts
            if lead_gen_options:
                prev_lg = dict(state_update.get("lead_gen_options") or {})
                prev_lg.update(lead_gen_options)
                state_update["lead_gen_options"] = prev_lg
            if email_campaign_context:
                prev_ec = dict(state_update.get("email_campaign_context") or {})
                prev_ec.update(email_campaign_context)
                state_update["email_campaign_context"] = prev_ec

        # Per-session overrides — set once, persisted in the checkpoint.
        if model:
            state_update["orchestrator_model_override"] = model
        if custom_instructions:
            state_update["custom_instructions"] = custom_instructions

        # Keep session credentials visible in both configurable and state paths.
        state_update["_session_credentials"] = session_credentials or {}

        # Permanent BYOK fallback: if the caller supplied no session-scoped
        # __llm__ override, hydrate one from the vault (keys written via
        # POST /api/v1/credentials scope=permanent). Session creds win.
        if "__llm__" not in state_update["_session_credentials"]:
            try:
                from ..vault.client import VaultClient

                vault = VaultClient()
                byok: dict[str, str] = {}
                for var in ("api_key", "base_url", "model"):
                    value = await vault.get_agent_env(user_id, "__llm__", var)
                    if value:
                        byok[var] = value
                if byok.get("api_key"):
                    state_update["_session_credentials"]["__llm__"] = byok
                    logger.info(
                        "run_turn: hydrated permanent BYOK __llm__ creds for user %s",
                        user_id,
                    )
            except Exception:
                logger.debug("run_turn: BYOK vault hydration failed", exc_info=True)

        await register_run(session_id)
        values_messages_sink: dict[str, Any] = {}
        current_task = asyncio.current_task()
        await self._set_active_stream_task(session_id, current_task)
        try:
            async for event in _yield_multistream_events(
                self._graph,
                state_update,
                config,
                values_messages_sink=values_messages_sink,
            ):
                yield event
        except asyncio.CancelledError:
            logger.info("run_turn: cancelled by kill-switch for session %s", session_id)
            from ..middleware.observers import emit_run_discarded

            await emit_run_discarded(session_id)
            yield {"type": "stopped", "session_id": session_id}
            return
        except Exception as exc:
            logger.exception("run_turn: graph stream error for session %s", session_id)
            from ..middleware.observers import emit_run_discarded

            await emit_run_discarded(session_id)
            yield _classify_stream_error(exc)
            return
        finally:
            await self._clear_active_stream_task(session_id, current_task)
            await unregister_run(session_id)
            # Persist even on error/cancel: a failed run is exactly what the
            # run audit (Verified Runs) is for. Never let a persist failure
            # break stream teardown.
            stream_msgs = values_messages_sink.get("messages")
            stream_list = stream_msgs if isinstance(stream_msgs, list) else None
            try:
                await self._persist_transcript(
                    session_id, session_credentials, stream_messages=stream_list
                )
            except Exception:
                logger.exception(
                    "run_turn: transcript persist failed for session %s", session_id
                )

        _cleanup_session_tmp(session_id)

        # If the graph suspended on an interrupt (HITL etc.), surface it on the
        # stream. With this LangGraph version, node-level interrupt() is recorded
        # in the checkpoint rather than raised out of astream, so read it back.
        pending = await _pending_interrupt_event(
            self._graph, {"configurable": {"thread_id": session_id}}
        )
        if pending is _CHECKPOINT_READ_FAILED:
            # Fail closed: we cannot tell whether the graph suspended, so do
            # NOT dispatch run-complete — sealing a possibly-suspended run
            # would attest steps for a run that never finished.
            logger.warning(
                "run_turn: checkpoint read failed for session %s; "
                "skipping run-complete dispatch",
                session_id,
            )
        elif pending is not None:
            yield pending
        else:
            # Run boundary: the turn finished without suspending. Dispatch
            # through the ExecutionObserver seam (run attestation, RFC 0003);
            # never raises, and is a no-op for the stock NoOpObserver.
            from ..middleware.observers import emit_run_complete

            await emit_run_complete(session_id)

        yield {"type": "done", "session_id": session_id}

    async def resume_from_interrupt(
        self,
        session_id: str,
        value: dict[str, Any],
        session_credentials: dict[str, dict[str, str]] | None = None,
        thread_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Resume a graph that is paused at an interrupt node.

        Args:
            session_id: The session / LangGraph thread to resume.
            value: The resume value dict — forwarded verbatim as the return value
                   of the ``interrupt()`` call that suspended the node.
                   Typically ``ResumePayload.value`` from ``internal_commons``.
        """
        config = _merge_graph_config(
            thread_config
            or {
                "configurable": {
                    "thread_id": session_id,
                    "session_credentials": session_credentials or {},
                }
            }
        )

        await register_run(session_id)
        values_messages_sink = {}
        current_task = asyncio.current_task()
        await self._set_active_stream_task(session_id, current_task)
        try:
            async for event in _yield_multistream_events(
                self._graph,
                Command(resume=value),
                config,
                values_messages_sink=values_messages_sink,
            ):
                yield event
        except asyncio.CancelledError:
            logger.info(
                "resume_from_interrupt: cancelled by kill-switch for session %s",
                session_id,
            )
            from ..middleware.observers import emit_run_discarded

            await emit_run_discarded(session_id)
            yield {"type": "stopped", "session_id": session_id}
            return
        except Exception as exc:
            logger.exception(
                "resume_from_interrupt: graph stream error for session %s", session_id
            )
            from ..middleware.observers import emit_run_discarded

            await emit_run_discarded(session_id)
            yield _classify_stream_error(exc)
            return
        finally:
            await self._clear_active_stream_task(session_id, current_task)
            await unregister_run(session_id)
            # Persist even on error/cancel: a failed run is exactly what the
            # run audit (Verified Runs) is for. Never let a persist failure
            # break stream teardown.
            stream_msgs = values_messages_sink.get("messages")
            stream_list = stream_msgs if isinstance(stream_msgs, list) else None
            try:
                await self._persist_transcript(
                    session_id, session_credentials, stream_messages=stream_list
                )
            except Exception:
                logger.exception(
                    "run_turn: transcript persist failed for session %s", session_id
                )

        _cleanup_session_tmp(session_id)

        # Run boundary after an HITL resume: only attest when the graph did not
        # suspend again on a further interrupt. Dispatch through the
        # ExecutionObserver seam; never raises, no-op for the stock observer.
        pending = await _pending_interrupt_event(
            self._graph, {"configurable": {"thread_id": session_id}}
        )
        if pending is _CHECKPOINT_READ_FAILED:
            # Fail closed: unknown suspend state — do not seal the run.
            logger.warning(
                "resume_from_interrupt: checkpoint read failed for session %s; "
                "skipping run-complete dispatch",
                session_id,
            )
        elif pending is None:
            from ..middleware.observers import emit_run_complete

            await emit_run_complete(session_id)

        yield {"type": "done", "session_id": session_id}

    async def get_status(
        self, session_id: str, thread_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return the current graph snapshot for a session."""
        config = thread_config or {"configurable": {"thread_id": session_id}}
        snapshot = await self._graph.aget_state(config)
        if not snapshot or not snapshot.values:
            return {"status": "not_found"}
        state = snapshot.values
        # LangGraph stores active interrupts in snapshot.tasks[*].interrupts
        # Extract the first interrupt payload so the frontend can reconstruct the UI after reload.
        active_interrupt: dict[str, Any] | None = None
        if snapshot.tasks:
            for task in snapshot.tasks:
                for intr in getattr(task, "interrupts", None) or []:
                    val = getattr(intr, "value", None)
                    if isinstance(val, dict):
                        active_interrupt = val
                        break
                if active_interrupt:
                    break
        is_interrupted = active_interrupt is not None
        checklist = state.get("task_checklist")
        captured = state.get("captured_workflow")
        artifacts = state.get("artifacts", {})
        pnd_candidates = state.get("pnd_candidates", [])

        return {
            "status": "interrupted" if is_interrupted else "ready",
            "session_id": session_id,
            "active_interrupt": active_interrupt,
            "estimated_token_count": state.get("estimated_token_count", 0),
            "artifacts": {
                k: {
                    "artifact_id": getattr(v, "artifact_id", k),
                    "filename": getattr(v, "filename", ""),
                    "mime_type": getattr(v, "mime_type", ""),
                    "size_bytes": getattr(v, "size_bytes", 0),
                }
                for k, v in artifacts.items()
            },
            "pnd_candidates": [
                {
                    "agent_id": cand_agent_id(c),
                    "agent_name": cand_agent_name(c),
                    "protocol_type": cand_protocol_type(c),
                }
                for c in pnd_candidates
            ],
            "task_checklist": (
                {
                    "checklist_id": _f(checklist, "checklist_id"),
                    "goal": _f(checklist, "goal"),
                    "version": _f(checklist, "version"),
                    "steps": [
                        {
                            "step_id": _f(s, "step_id"),
                            "description": _f(s, "description"),
                            "status": _f(s, "status"),
                            "agent_id": _f(s, "agent_id"),
                            "result_summary": _f(s, "result_summary"),
                            "call_id": _f(s, "call_id"),
                            "tool_name_resolved": _f(s, "tool_name_resolved"),
                            "started_at": _f(s, "started_at"),
                            "completed_at": _f(s, "completed_at"),
                        }
                        for s in (_f(checklist, "steps") or [])
                    ],
                }
                if checklist
                else None
            ),
            "captured_workflow": (
                {
                    "name": _f(captured, "name"),
                    "goal_template": _f(captured, "goal_template"),
                    "steps": _f(captured, "steps", []),
                    "agents_used": _f(captured, "agents_used", []),
                    "parameters": _f(captured, "parameters", {}),
                }
                if captured
                else None
            ),
            "lead_gen_options": state.get("lead_gen_options") or {},
            "email_campaign_context": state.get("email_campaign_context") or {},
        }

    async def patch_session_context(
        self,
        session_id: str,
        *,
        lead_gen_options: dict[str, Any] | None = None,
        email_campaign_context: dict[str, Any] | None = None,
        thread_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge session-scoped CRM / campaign dicts without running a chat turn."""
        config = _merge_graph_config(
            thread_config or {"configurable": {"thread_id": session_id}}
        )
        snapshot = await self._graph.aget_state(config)
        if not snapshot or not snapshot.values:
            return {"ok": False, "error": "not_found"}

        cur = snapshot.values
        updates: dict[str, Any] = {}
        if lead_gen_options:
            merged = dict(cur.get("lead_gen_options") or {})
            merged.update(lead_gen_options)
            updates["lead_gen_options"] = merged
        if email_campaign_context:
            merged = dict(cur.get("email_campaign_context") or {})
            merged.update(email_campaign_context)
            updates["email_campaign_context"] = merged
        if not updates:
            return {"ok": True, "merged": {}}

        aupdate = getattr(self._graph, "aupdate_state", None)
        if callable(aupdate):
            await aupdate(config, updates)
            return {"ok": True, "merged": updates}

        update_state = getattr(self._graph, "update_state", None)
        if callable(update_state):
            update_state(config, updates)
            return {"ok": True, "merged": updates}

        logger.error("LangGraph compiled graph has no aupdate_state/update_state")
        return {"ok": False, "error": "patch_unsupported"}


def _classify_stream_error(exc: BaseException) -> dict[str, Any]:
    """Map a graph-stream exception to a typed, user-facing error event.

    The graph stream used to surface every failure as the same opaque
    "Internal graph error" string, so the UI always offered a blanket retry —
    even when retrying could not help (exhausted LLM credits, step-budget
    overflow). This distinguishes transient failures (retry is sensible) from
    fatal ones, and gives the frontend a ``retriable`` flag + ``category``.

    Type detection is by name/attribute rather than hard imports so the runner
    keeps no dependency on optional LLM/HTTP client packages.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )

    # LLM provider credits exhausted (OpenRouter/OpenAI 402) — retry won't help.
    if status == 402 or "PaymentRequired" in name:
        return {
            "type": "error",
            "category": "llm_credits",
            "retriable": False,
            "error": (
                "LLM provider credits exhausted (402). "
                "Top up OpenRouter credits, then try again."
            ),
        }

    # The orchestration loop hit its recursion/step budget.
    if "GraphRecursion" in name:
        return {
            "type": "error",
            "category": "step_budget",
            "retriable": False,
            "error": (
                "This run hit its step budget before finishing. "
                "Try a narrower goal or fewer agents."
            ),
        }

    # Rate-limited or transient upstream/server errors — a retry may succeed.
    if status in (408, 409, 425, 429, 500, 502, 503, 504) or name in (
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectError",
        "APITimeoutError",
        "RateLimitError",
    ):
        return {
            "type": "error",
            "category": "transient",
            "retriable": True,
            "error": "A transient upstream error interrupted the run. Please retry.",
        }

    # Unknown/internal — don't promise a retry will help.
    return {
        "type": "error",
        "category": "internal",
        "retriable": False,
        "error": "Internal graph error — see server logs.",
    }


def _extract_events(
    chunk: dict[str, Any],
    *,
    skip_aimessage_token: bool = False,
    seen_invocation: set[tuple[Any, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Extract structured typed events from a LangGraph state chunk (values mode)."""
    events: list[dict[str, Any]] = []
    inv_seen = seen_invocation if seen_invocation is not None else set()

    # Belt-and-suspenders: replay invocation payloads if custom stream dropped them
    pending = chunk.get("_pending_events") or []
    for p in pending:
        if not isinstance(p, dict):
            continue
        if p.get("type") not in _AGENT_INVOCATION_TYPES:
            continue
        key = _invocation_dedupe_key(p)
        if key in inv_seen:
            continue
        inv_seen.add(key)
        events.append(p)

    # Fallback token when ``messages`` stream emitted no text deltas (e.g. mocks).
    messages_list = chunk.get("messages", [])
    if not skip_aimessage_token and messages_list:
        last = messages_list[-1]
        if isinstance(last, AIMessage):
            content = getattr(last, "content", "")
            tool_calls = getattr(last, "tool_calls", None)
            if isinstance(content, str) and content and not tool_calls:
                events.append({"type": "token", "content": content})

    # checklist_snapshot — emitted whenever task_checklist is present in chunk
    checklist = chunk.get("task_checklist")
    if checklist is not None:
        events.append(
            {
                "type": "checklist_snapshot",
                "checklist_id": _f(checklist, "checklist_id"),
                "goal": _f(checklist, "goal"),
                "version": _f(checklist, "version"),
                "steps": [
                    {
                        "step_id": _f(s, "step_id"),
                        "description": _f(s, "description"),
                        "status": _f(s, "status"),
                        "agent_id": _f(s, "agent_id"),
                        "result_summary": _f(s, "result_summary"),
                        "call_id": _f(s, "call_id"),
                        "tool_name_resolved": _f(s, "tool_name_resolved"),
                        "started_at": _f(s, "started_at"),
                        "completed_at": _f(s, "completed_at"),
                    }
                    for s in (_f(checklist, "steps") or [])
                ],
            }
        )

    # agents_discovered — new PnD candidates
    pnd_candidates = chunk.get("pnd_candidates")
    if pnd_candidates:
        events.append(
            {
                "type": "agents_discovered",
                "agents": [
                    {
                        "agent_id": cand_agent_id(c),
                        "agent_name": cand_agent_name(c),
                        "protocol_type": cand_protocol_type(c),
                    }
                    for c in pnd_candidates
                ],
            }
        )

    # token_usage
    token_count = chunk.get("estimated_token_count")
    if token_count is not None:
        events.append({"type": "token_usage", "estimated_token_count": token_count})

    # artifact_created — one event per artifact in the dict
    artifacts = chunk.get("artifacts")
    if artifacts:
        for artifact_id, artifact in artifacts.items():
            events.append(
                {
                    "type": "artifact_created",
                    "artifact_id": artifact_id,
                    "mime_type": getattr(artifact, "mime_type", ""),
                    "filename": getattr(artifact, "filename", ""),
                    "size_bytes": getattr(artifact, "size_bytes", 0),
                }
            )

    return events
