"""Shared test fixtures for SuperAgent tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from superagent.graph.builder import build_superagent_graph
from superagent.graph.state import default_state
from superagent.pnd.models import (
    CandidateCapability,
    PnDCandidateResponse,
    ToolCandidate,
)


@pytest.fixture(autouse=True)
def _allow_ephemeral_attestation_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that seal envelopes do not need a persistent seed."""
    monkeypatch.setenv("ATTESTATION_ALLOW_EPHEMERAL_KEY", "1")


# ── LLM mocks ─────────────────────────────────────────────────────────────────


def make_text_response(text: str) -> MagicMock:
    """Build a mock OpenAI chat completion response with plain text."""
    choice = MagicMock()
    choice.message.content = text
    choice.message.tool_calls = None
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def make_tool_call_response(
    tool_name: str, args: dict[str, Any], call_id: str = "call_001"
) -> MagicMock:
    """Build a mock OpenAI chat completion response with a tool call."""
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(args)
    choice = MagicMock()
    choice.message.content = ""
    choice.message.tool_calls = [tc]
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.fixture
def mock_orchestrator_llm():
    """MagicMock ChatOpenAI-style client (astream + bind_tools) for the orchestrator."""
    from unittest.mock import MagicMock

    from langchain_core.messages import AIMessageChunk

    mock_chat = MagicMock()

    async def fake_astream(*_a, **_kw):
        yield AIMessageChunk(content="Hello! How can I help you?")

    mock_chat.astream = fake_astream
    mock_chat.bind_tools = MagicMock(return_value=mock_chat)
    return mock_chat


@pytest.fixture
def mock_small_llm():
    """AsyncMock for the small (Haiku) classifier LLM."""
    mock = AsyncMock()
    choice = MagicMock()
    choice.message.content = "NO"
    resp = MagicMock()
    resp.choices = [choice]
    mock.chat.completions.create = AsyncMock(return_value=resp)
    return mock


# ── PnD fixture ────────────────────────────────────────────────────────────────


@pytest.fixture
def gmail_candidate() -> ToolCandidate:
    return ToolCandidate(
        agent_id="did:orcha:agent:gmail-mcp-001",
        agent_name="Gmail MCP",
        agent_description="Read and send Gmail emails via MCP",
        protocol_type="MCP",
        relevance_score=0.92,
        capabilities=[
            CandidateCapability(
                capability_id="list_emails",
                capability_type="TOOL",
                name="list_emails",
                description="List unread emails",
                input_schema={
                    "type": "object",
                    "properties": {"max_results": {"type": "integer"}},
                },
            ),
            CandidateCapability(
                capability_id="send_email",
                capability_type="TOOL",
                name="send_email",
                description="Send an email",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
        ],
    )


@pytest.fixture
def mock_pnd_client(gmail_candidate):
    """AsyncMock PnD client returning a single Gmail MCP candidate."""
    mock = AsyncMock()
    mock.get_candidates = AsyncMock(
        return_value=PnDCandidateResponse(
            candidates=[gmail_candidate],
            retrieval_latency_ms=42,
        )
    )
    return mock


# ── Graph fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def memory_graph():
    """SuperAgent graph using MemorySaver (no Redis required)."""
    from langgraph.checkpoint.memory import MemorySaver
    from superagent.system_tools.registry import register_all_system_tools

    register_all_system_tools()
    return build_superagent_graph(checkpointer_override=MemorySaver())


@pytest.fixture
def session_state():
    return default_state(session_id="test-session-001", user_id="test-user-001")
