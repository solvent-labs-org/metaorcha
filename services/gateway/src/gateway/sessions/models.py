"""Session request/response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateSessionBody(BaseModel):
    """Optional title from client (truncated first prompt)."""

    title: str | None = Field(default=None, max_length=200)


class CreateSessionResponse(BaseModel):
    session_id: str


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=32768)
    artifact_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(
        default=None,
        description="Per-turn orchestrator model override (e.g. from the model picker).",
    )
    custom_instructions: str | None = Field(
        default=None,
        max_length=2000,
        description="Per-session operator instructions appended to the orchestrator system prompt.",
    )
    acceptance_criteria: dict[str, Any] | None = Field(
        default=None,
        description="Optional machine-checkable acceptance criteria for this turn.",
    )


class ResumeRequest(BaseModel):
    interrupt_id: str
    interrupt_type: str
    value: dict[str, Any] = Field(
        default_factory=dict,
        description="Resume value forwarded verbatim to the suspended interrupt() call.",
    )


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str  # "ready" | "interrupted" | "not_found"
    active_interrupt: Any | None = None
    """Full InterruptEvent dict when interrupted; None otherwise."""
    estimated_token_count: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    pnd_candidates: list[Any] = Field(default_factory=list)
    task_checklist: Any | None = None
    captured_workflow: Any | None = None


class SessionStopResponse(BaseModel):
    ok: bool
    status: Literal["stopping", "not_running"]


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
