"""Request/Response Pydantic models for SuperAgent API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    user_id: str
    office_id: str | None = Field(
        default=None,
        description="Office the session belongs to (set by Gateway); fixed at creation.",
    )
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
    # None when the step carries no structural verdict (a blocked, unresolved
    # or system call, or a row persisted before verdicts existed): unchecked,
    # never counted as verified.
    verified: bool | None = None
    verdict_reason: str = ""
    base_fee: str | None = None
    total_cost_usd: str | None = None


class RunAuditSummary(BaseModel):
    total_steps: int
    steps_verified: int
    steps_failed: int
    steps_unchecked: int = 0
    protocols: list[str]
    total_cost_usd: str
    duration_ms: int | None = None


class RunAuditGate(BaseModel):
    """The settle gate's deciding row for the session's latest sealed run.

    Keyed by that run's ``run_id`` (``attested_settlements``), never by the
    session: a later, unjudged run never inherits an earlier run's outcome.
    Present only when a gate evaluated that run; ``failed_checks`` are gate
    ids (story 1.1, FR-14).
    """

    outcome: str  # settled | refused
    failed_checks: list[str]
    envelope_digest: str
    created_at: str
    # False for a verdict-only evaluation (``call_id`` NULL, AD-12): the run
    # was judged and nothing was charged. "settled" then moves no money.
    charged: bool = True


class RunAuditSettlement(BaseModel):
    """What a routine firing's sealed run amounts to in settlement (story 2.6).

    Present only for a firing's session whose run sealed. ``state`` is the
    firing row's, read and never recomputed (AD-22). With no gate decision
    recorded, the block says so and carries no field that implies one.
    """

    run_id: str
    state: str
    label: str
    gate_evaluated: bool
    verdict_only: bool | None = None
    failed_checks: list[str] | None = None  # gate ids, from the ledger row
    failed_verdicts: list[dict[str, str]] | None = (
        None  # the signed verdicts that failed
    )
    checks: str | None = None  # unchecked | checked | not_evaluated
    checks_label: str | None = None
    statement: str


class RunAuditFiring(BaseModel):
    """The routine firing this session belongs to, as its row records it."""

    routine_id: str
    state: str
    label: str
    detail: str | None = None


class RunAuditCoverage(BaseModel):
    """What the export and its signed receipt cover (FR-8, story 3.2).

    ``statement`` is the verifier's own coverage text
    (``emerge.run_attestation.coverage_statement``), computed from the
    envelope at export time and never stored; ``export`` says how this
    export's step list relates to the signed receipt.
    """

    statement: str
    run_id: str | None = None
    receipt_steps: int | None = None
    receipt_tools: list[str] = Field(default_factory=list)
    export: str


class RunAuditResponse(BaseModel):
    session_id: str
    generated_at: str
    goal: str
    summary: RunAuditSummary
    steps: list[RunAuditStep]
    note: str
    coverage: RunAuditCoverage
    # Story 3.3: the sealed run's signed ``model`` verdicts ("<route>/<model
    # id>", first-use order); None when no receipt was read, [] when the
    # receipt carries none (sealed before the verdict existed).
    models: list[str] | None = None
    run_id: str | None = None
    gate: RunAuditGate | None = None
    settlement: RunAuditSettlement | None = None
    firing: RunAuditFiring | None = None


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
    office_id: str | None = None


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
