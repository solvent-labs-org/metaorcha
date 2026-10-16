from __future__ import annotations

from typing import Any

from metabot.events import ToolCall, ToolResult
from metabot.steps import StepRecorder


def test_pairs_and_latency():
    rec = StepRecorder("claude-code")
    rec.on_event(ToolCall("t1", "Bash", {"command": "ls"}))
    rec.on_event(ToolResult("t1", "a\nb", True))
    [step] = rec.steps()
    assert step["call_id"] == "t1"
    assert step["tool"] == "claude-code/Bash"
    assert step["args"] == {"command": "ls"}
    assert step["output"] == "a\nb"
    assert step["success"] is True
    assert isinstance(step["latency_ms"], int)
    assert step["latency_ms"] >= 0


def test_unpaired_call_is_failed_step():
    rec = StepRecorder("codex")
    rec.on_event(ToolCall("i0", "command_execution", {"command": "sleep 9"}))
    [step] = rec.steps()
    assert step["success"] is False
    assert step["output"] == {"error": "no result"}


def test_skip_tools_by_prefix():
    rec = StepRecorder("claude-code", skip_tools=("mcp__metabot__",))
    rec.on_event(ToolCall("m1", "mcp__metabot__gh__list_issues", {}))
    rec.on_event(ToolResult("m1", "[]", True))
    rec.on_event(ToolCall("t2", "Read", {"file_path": "a"}))
    rec.on_event(ToolResult("t2", "x", True))
    assert [s["tool"] for s in rec.steps()] == ["claude-code/Read"]


def test_external_steps_keep_arrival_order():
    rec = StepRecorder("claude-code")
    rec.on_event(ToolCall("t1", "Bash", {"command": "ls"}))
    rec.add_external(
        {
            "call_id": "sw-1",
            "tool": "did:orcha:agent:conn-abc#list_issues",
            "args": {},
            "output": [],
            "success": True,
            "latency_ms": 3,
        }
    )
    rec.on_event(ToolResult("t1", "ok", True))
    assert [s["call_id"] for s in rec.steps()] == ["t1", "sw-1"]
    assert rec.steps()[1]["tool"] == "did:orcha:agent:conn-abc#list_issues"


def test_ids_are_made_printable_ascii():
    rec = StepRecorder("cursor")
    raw_id = "call-dd0a\nfc_01é"
    rec.on_event(ToolCall(raw_id, "shellToolCall", {"command": "echo"}))
    rec.on_event(ToolResult(raw_id, {"success": {}}, True))
    [step] = rec.steps()
    assert step["call_id"] == "call-dd0a_fc_01_"
    assert step["success"] is True


def test_non_dict_args_are_wrapped():
    rec = StepRecorder("codex")
    args: Any = ["a"]  # not a dict; the recorder wraps it
    rec.on_event(ToolCall("x", "weird", args))
    rec.on_event(ToolResult("x", "ok", True))
    assert rec.steps()[0]["args"] == {"value": ["a"]}


def test_reused_id_keeps_both_calls():
    rec = StepRecorder("codex")
    rec.on_event(ToolCall("dup", "command_execution", {"command": "a"}))
    rec.on_event(ToolCall("dup", "command_execution", {"command": "b"}))
    rec.on_event(ToolResult("dup", "done", True))
    steps = rec.steps()
    assert [s["args"]["command"] for s in steps] == ["a", "b"]
    assert [s["success"] for s in steps] == [False, True]
