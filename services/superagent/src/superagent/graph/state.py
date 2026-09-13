"""AgentState TypedDict and supporting dataclasses."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.messages import BaseMessage  # noqa: TC002

from ..startup.platform_mcp_baseline import get_baseline_candidates

# ── Reducer helpers ────────────────────────────────────────────────────────────


def merge_agent_call_results(
    existing: dict[str, Any], new: dict[str, Any]
) -> dict[str, Any]:
    """Merge tool call results — later values overwrite earlier ones."""
    return {**existing, **new}


def append_checklist_history(existing: list[Any], new: list[Any]) -> list[Any]:
    """Append new history entries (never truncate)."""
    return existing + new


def merge_pnd_candidates(existing: list[Any], new: list[Any]) -> list[Any]:
    """Merge PnD candidates by agent_id — new entries take precedence, existing kept.

    This ensures the tool set only grows during a session: once an agent is
    discovered it stays available, and fresher results for the same agent_id
    replace the stale ones.
    """
    if not new:
        return existing
    merged: dict[str, Any] = {
        getattr(c, "agent_id", None) or c.get("agent_id", ""): c for c in existing
    }
    for c in new:
        agent_id = getattr(c, "agent_id", None) or c.get("agent_id", "")
        merged[agent_id] = c
    return list(merged.values())


# ── Supporting dataclasses ─────────────────────────────────────────────────────


@dataclass
class ChecklistStep:
    step_id: str
    description: str
    status: str = "pending"  # "pending" | "in_progress" | "done" | "failed"
    agent_id: str | None = None
    result_summary: str | None = None
    # Structured execution tracking — populated by execute_agent_calls_node
    call_id: str | None = None
    tool_name_resolved: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class TaskChecklist:
    checklist_id: str
    goal: str
    steps: list[ChecklistStep] = field(default_factory=list)
    version: int = 1
    scope_hash: str = ""  # hash of goal — changes trigger step reset


@dataclass
class ArtifactRef:
    artifact_id: str
    filename: str
    mime_type: str
    size_bytes: int
    s3_bucket: str
    s3_key: str  # artifacts/{user_id}/{artifact_id}/{filename}


@dataclass
class CapturedWorkflow:
    name: str
    goal_template: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    agents_used: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)


# ── AgentState ─────────────────────────────────────────────────────────────────


class AgentState(dict):  # type: ignore[type-arg]
    """
    Typed AgentState for LangGraph.

    Using TypedDict notation via Annotated for reducer declarations.
    Inherits from dict so LangGraph can serialise it without custom codec.
    """

    # Accumulated across turns (operator.add appends)
    messages: Annotated[list[BaseMessage], operator.add]
    # Merged per tool_call_id (merge_agent_call_results)
    agent_call_result_store: Annotated[dict[str, Any], merge_agent_call_results]
    # Replaced per-turn (no reducer — last write wins)
    task_checklist: TaskChecklist | None
    # History appended (append_checklist_history)
    checklist_history: Annotated[list[Any], append_checklist_history]
    # Immutable after session creation
    session_id: str
    user_id: str
    # Updated by orchestrator each turn
    estimated_token_count: int
    # Artifact refs accumulated
    artifacts: dict[str, ArtifactRef]
    # Captured workflow (set by save_as_workflow_template system tool)
    captured_workflow: CapturedWorkflow | None
    # Merged across turns — new candidates appended, same agent_id replaces stale entry
    pnd_candidates: Annotated[list[Any], merge_pnd_candidates]
    # Passed to Lead Gen A2A as a JSON-RPC data part (crm_type, write_to_crm, …).
    lead_gen_options: Annotated[dict[str, Any], merge_agent_call_results]
    # Session-scoped memory for outbound campaigns (ICP, tone, last task id) — orchestrator prompt.
    email_campaign_context: Annotated[dict[str, Any], merge_agent_call_results]
    # Per-session orchestrator model override (from the model picker); None = env default.
    orchestrator_model_override: str | None
    # Per-session operator instructions appended to the orchestrator system prompt.
    custom_instructions: str | None
    # Session-scoped credentials forwarded by the gateway (agent_id → var → value).
    # Declared so LangGraph does not drop the key from state updates; read by
    # middleware (auth cascade) and the orchestrator (BYOK `__llm__` entry).
    _session_credentials: dict[str, dict[str, str]]
    # Ephemeral SSE queue from execute_agent_calls — drained in runner; cleared by orchestrator
    _pending_events: list[Any]
    # In-memory OAuth grant cache: scope_key → True. Persisted in LangGraph state so
    # grants survive across node re-executions without a Redis round-trip.
    _agent_oauth_grants: dict[str, bool]
    # Actual completion_tokens from the orchestrator LLM call that produced this turn's tool calls.
    # Written by orchestrator_llm_node; consumed by ExecutionMiddleware for PLATFORM_TOKEN_RATE billing.
    _last_turn_tokens: int
    # Per-turn declared acceptance criteria (hashed into policy_version).
    _declared_criteria: dict[str, Any] | None
    # Active DAG plan (Slice 1) — plain JSON-safe dict built by
    # nodes/dag_plan.new_active_plan. None on the stock ReAct path.
    active_plan: dict[str, Any] | None


def default_state(session_id: str, user_id: str) -> dict[str, Any]:
    """Return the initial state dict for a new session."""
    return {
        "messages": [],
        "agent_call_result_store": {},
        "task_checklist": None,
        "checklist_history": [],
        "session_id": session_id,
        "user_id": user_id,
        "estimated_token_count": 0,
        "artifacts": {},
        "captured_workflow": None,
        "pnd_candidates": get_baseline_candidates(),
        "lead_gen_options": {},
        "email_campaign_context": {},
        "orchestrator_model_override": None,
        "custom_instructions": None,
        "_session_credentials": {},
        "_pending_events": [],
        "_agent_oauth_grants": {},
        "_last_turn_tokens": 0,
        "_declared_criteria": None,
        "active_plan": None,
    }
