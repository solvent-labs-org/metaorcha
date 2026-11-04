from __future__ import annotations

from typing import Any

import pytest

from metabot.events import ToolCall, ToolResult
from metabot.steps import MAY_HAVE_RUN, StepRecorder


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


SHELL = ToolCall("call_0", "shell", {"command": "make deploy"})
BOARD = ToolCall("call_0", "metabot/gh__list_issues", {})  # the switchboard records it
BOARDS = ToolResult("call_0", {"content": [{"type": "text", "text": "[]"}]}, True)
SHELLS = ToolResult("call_0", {"error": "exit 2"}, False)


@pytest.mark.parametrize(
    "events",
    [
        [SHELL, BOARD, BOARDS, SHELLS],  # side by side, the switchboard's answer first
        [SHELL, BOARD, SHELLS, BOARDS],  # side by side, the engine's own answer first
        [SHELL, BOARD, BOARDS],  # in turn: the engine's own call never got its answer
        [BOARD, SHELL, BOARDS, SHELLS],  # the switchboard's call first
    ],
    ids=["switchboard-first", "own-first", "own-unanswered", "board-call-first"],
)
def test_an_own_call_never_takes_an_answer_under_a_switchboard_call_s_id(events):
    rec = StepRecorder("mine", skip_tools=("metabot/",), unanswered=MAY_HAVE_RUN)
    for event in events:
        rec.on_event(event)
    [step] = rec.steps()  # the switchboard records its call itself
    assert step["tool"] == "mine/shell" and step["args"] == {"command": "make deploy"}
    # which answer was whose cannot be told, so it has none: before, it could take the
    # switchboard call's, and say it succeeded
    assert step["output"] == MAY_HAVE_RUN and step["success"] is False


def test_a_switchboard_call_s_answer_leaves_another_id_s_call_alone():
    rec = StepRecorder("mine", skip_tools=("metabot/",), unanswered=MAY_HAVE_RUN)
    rec.on_event(ToolCall("t1", "shell", {"command": "ls"}))
    rec.on_event(ToolCall("b1", "metabot/gh__list_issues", {}))
    rec.on_event(ToolResult("b1", "[]", True))
    rec.on_event(ToolResult("t1", "a\nb", True))
    [step] = rec.steps()
    assert (step["call_id"], step["output"], step["success"]) == ("t1", "a\nb", True)


@pytest.mark.parametrize("answered", [True, False])  # the bridge reports the board's answer
def test_a_bridge_that_gives_each_call_its_own_id_keeps_its_own_answers(answered):
    rec = StepRecorder("mine", skip_tools=("metabot/",), unanswered=MAY_HAVE_RUN)
    for turn in range(2):  # metabot.bridge/1: each call has an id no other call in the run has
        rec.on_event(ToolCall(f"b{turn}", "metabot/gh__list_issues", {}))
        if answered:
            rec.on_event(ToolResult(f"b{turn}", "[]", True))
        rec.on_event(ToolCall(f"t{turn}", "shell", {"command": "ls"}))
        rec.on_event(ToolResult(f"t{turn}", "a\nb", True))
    assert [(s["call_id"], s["success"]) for s in rec.steps()] == [("t0", True), ("t1", True)]
