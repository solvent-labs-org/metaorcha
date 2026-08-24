"""Transcript persistence: checkpoint message coercion."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from src.generated_client import enums
from superagent.persistence.transcript_store import (
    TRANSCRIPT_TOOL_META_KEY,
    coerce_checkpoint_messages,
    coerce_checkpoint_messages_for_persist,
    messages_to_entry_dicts,
)


def test_coerce_langchain_serialized_dicts():
    human_d = {"type": "human", "data": {"content": "hi"}}
    ai_d = {"type": "ai", "data": {"content": "hello"}}
    coerced = coerce_checkpoint_messages([human_d, ai_d])
    assert len(coerced) == 2
    assert isinstance(coerced[0], HumanMessage)
    assert isinstance(coerced[1], AIMessage)
    assert coerced[0].content == "hi"
    assert coerced[1].content == "hello"


def test_coerce_mixed_objects_and_dicts():
    coerced = coerce_checkpoint_messages(
        [HumanMessage(content="x"), {"type": "ai", "data": {"content": "y"}}]
    )
    assert len(coerced) == 2
    assert coerced[0].content == "x"
    assert coerced[1].content == "y"


def test_coerce_openai_style_assistant_and_tool():
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
        ],
    }
    tool = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "result",
        "name": "search",
    }
    coerced = coerce_checkpoint_messages([assistant, tool])
    assert len(coerced) == 2
    assert isinstance(coerced[0], AIMessage)
    assert coerced[0].tool_calls
    assert isinstance(coerced[1], ToolMessage)
    assert coerced[1].tool_call_id == "call_1"


def test_coerce_for_persist_inserts_placeholder_for_bad_dict():
    bad = {"not_a_message": True}
    out = coerce_checkpoint_messages_for_persist([HumanMessage(content="u"), bad])
    assert len(out) == 2
    assert isinstance(out[0], HumanMessage)
    assert isinstance(out[1], AIMessage)
    assert "could not parse" in (out[1].content or "").lower()


def test_tool_row_persists_display_name_and_transcript_meta():
    m = ToolMessage(
        content="Error: missing key",
        tool_call_id="call-abc",
        name="Notion MCP",
        additional_kwargs={
            TRANSCRIPT_TOOL_META_KEY: {
                "agent_id": "did:orcha:agent:finance-dashboard-agent",
                "capability_id": "write_page",
                "protocol": "MCP",
                "internal_tool_name": "finance_dashboard_agent__get_portfolio",
                "invocation_args": {"title": "x"},
            }
        },
    )
    rows = messages_to_entry_dicts([m], 1)
    assert len(rows) == 1
    assert rows[0]["role"] == enums.TranscriptRole.TOOL
    assert rows[0]["tool_name"] == "Notion MCP"
    assert (
        rows[0]["tool_inputs"]["agent_id"] == "did:orcha:agent:finance-dashboard-agent"
    )
    assert rows[0]["tool_inputs"]["invocation_args"] == {"title": "x"}
