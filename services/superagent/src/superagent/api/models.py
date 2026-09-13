"""Request/Response Pydantic models for SuperAgent API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    user_id: str
    title: str | None = Field(
        default=None,
        description="Display title; truncated first prompt from client.",
    )


class CreateSessionResponse(BaseModel):
    session_id: str


class ToolCallPart(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TranscriptEntryDTO(BaseModel):
    sequence_num: int
    role: Literal["USER", "ASSISTANT", "TOOL"]
    content: str
    tool_calls: list[ToolCallPart] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_inputs: dict[str, Any] | None = None
    tool_status: Literal["success", "error"] | None = None
    created_at: str


class TranscriptListResponse(BaseModel):
    entries: list[TranscriptEntryDTO]


class RunAuditStep(BaseModel):
    seq: int
    agent_id: str
    capability_id: str = ""
    protocol: str = ""
    internal_tool_name: str = ""
    verified: bool = True
    verdict_reason: str = "ok"
    base_fee: str | None = None
    total_cost_usd: str | None = None


class RunAuditSummary(BaseModel):
    total_steps: int
    steps_verified: int
    steps_failed: int
    protocols: list[str]
    total_cost_usd: str
    duration_ms: int | None = None


class RunAuditResponse(BaseModel):
    session_id: str
    generated_at: str
    goal: str
    summary: RunAuditSummary
    steps: list[RunAuditStep]
    note: str


class ConversationSessionSummaryDTO(BaseModel):
    session_id: str
    title: str
    updated_at: str


class PaginatedSessionsResponse(BaseModel):
    items: list[ConversationSessionSummaryDTO]
    page: int
    page_size: int
    total: int
    has_next: bool


class MessageRequest(BaseModel):
    user_id: str
    message: str = Field(..., min_length=1, max_length=32768)
    session_credentials: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Per-session agent credentials injected by Gateway. "
        "Keys are agent_id, values are {VAR_NAME: plaintext_value} dicts. "
        "These are injected into LangGraph config and never persisted.",
    )
    artifact_ids: list[str] = Field(
        default_factory=list,
        description="IDs of artifacts uploaded before this message turn. "
        "SuperAgent pre-loads these into AgentState.artifacts so the LLM "
        "can reference them in the [ARTIFACTS IN SESSION] prompt block.",
    )
    lead_gen_options: dict[str, Any] = Field(
        default_factory=dict,
        description="Shallow-merge into session state for the next turn — forwarded "
        "to Lead Gen over A2A (e.g. crm_type hubspot|gsheets|notion|excel, write_to_crm, max_leads).",
    )
    email_campaign_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque session memory for ongoing outreach (e.g. campaign name, tone, "
        "last delegated task summary). Surfaced to the orchestrator system prompt.",
    )
    model: str | None = Field(
        default=None,
        description="Per-session orchestrator model override. When set, the orchestrator "
        "LLM node uses this model instead of ORCHESTRATOR_MODEL for the turn.",
    )
    custom_instructions: str | None = Field(
        default=None,
        max_length=2000,
        description="Per-session operator instructions appended as a delimited section "
        "to the orchestrator system prompt.",
    )
    acceptance_criteria: dict[str, Any] | None = Field(
        default=None,
        description="Optional machine-checkable acceptance criteria for this turn. "
        "Hashed into policy_version as +criteria:<64-hex>. Unknown keys are 422.",
    )

    @field_validator("acceptance_criteria")
    @classmethod
    def _known_acceptance_criteria(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        # Local import: models must not pull middleware at module import.
        from superagent.middleware.criteria import SUPPORTED_CRITERIA

        if value is None:
            return value
        for key in value:
            if key not in SUPPORTED_CRITERIA:
                raise ValueError(f"unsupported criterion: {key}")
        return value


class ResumeRequest(BaseModel):
    user_id: str
    value: dict[str, Any] = Field(
        ...,
        description="Resume payload forwarded verbatim as the return value "
        "of the interrupt() call that suspended the node. "
        "Typically the contents of ResumePayload.value from internal_commons.",
    )
    session_credentials: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Per-session agent credentials injected by Gateway.",
    )


class SessionDetailResponse(BaseModel):
    session_id: str
    user_id: str


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str  # "ready" | "interrupted" | "not_found"
    active_interrupt: dict[str, Any] | None = None
    """Full InterruptEvent dict when the session is interrupted; None otherwise.
    Populated from the LangGraph checkpoint so the frontend can reconstruct
    the interrupt UI after a page reload."""
    estimated_token_count: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    pnd_candidates: list[Any] = Field(default_factory=list)
    task_checklist: Any | None = None
    captured_workflow: Any | None = None
    lead_gen_options: dict[str, Any] = Field(default_factory=dict)
    email_campaign_context: dict[str, Any] = Field(default_factory=dict)


class SessionContextPatchRequest(BaseModel):
    """Merge structured session fields without sending a user message."""

    lead_gen_options: dict[str, Any] | None = Field(
        default=None,
        description="Shallow-merge into existing lead_gen_options on the graph checkpoint.",
    )
    email_campaign_context: dict[str, Any] | None = Field(
        default=None,
        description="Shallow-merge into existing email_campaign_context.",
    )


class SessionContextPatchResponse(BaseModel):
    ok: bool
    merged: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class SessionStopResponse(BaseModel):
    ok: bool
    status: Literal["stopping", "not_running"]


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "superagent"


# ── Agent credential management ───────────────────────────────────────────────


class StoreAgentEnvRequest(BaseModel):
    """Store env-var credentials for a STDIO MCP agent."""

    user_id: str
    agent_id: str
    credentials: dict[str, str] = Field(
        ...,
        description="Mapping of env-var name → plaintext value. "
        "e.g. {'NOTION_API_KEY': 'secret_abc123'}",
    )


class AgentEnvStatusResponse(BaseModel):
    """Per-var existence status — values are never returned."""

    agent_id: str
    status: dict[str, str] = Field(
        ...,
        description="Mapping of env-var name → 'configured' | 'missing'",
    )
