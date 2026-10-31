"""A CLI engine's call through the switchboard is in the record once, where a limit stops it.

The engine is a stand-in codex (``fake_engines/fake_codex_mcp.py``), or a stand-in bridge
(``fake_engines/fake_bridge_mcp.py``), that makes its call through the real
``metabot mcp serve``, so the switchboard records it as it does for a real one.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from conftest import FAKE_MCP, TESTS
from metabot.approvals import DenyAll
from metabot.connections import ConnectionStore
from metabot.engines.base import EngineJob
from metabot.engines.bridge import BridgeEngine
from metabot.engines.codex import CodexEngine
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.runner import Limits, RunCancelled, run_job
from metabot.steps import MAY_HAVE_RUN

FAKE_CODEX = TESTS / "fake_engines" / "fake_codex_mcp.py"
FAKE_BRIDGE = TESTS / "fake_engines" / "fake_bridge_mcp.py"


class _Codex(CodexEngine):
    def argv(self, job: EngineJob) -> list[str]:
        return [sys.executable, str(FAKE_CODEX), *super().argv(job)[1:]]


async def _cut_off(tmp_path, monkeypatch, mode: str, tool: str, *server: str) -> list[dict]:
    count = tmp_path / "count"
    ConnectionStore().add(
        name="gh", transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count), *server],
    )  # fmt: skip
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("FAKE_CODEX_TOOL", tool)
    monkeypatch.setenv("FAKE_CODEX_ARGS", json.dumps({"title": "t1"} if "create" in tool else {}))
    monkeypatch.setenv("FAKE_CODEX_COUNT", str(count))
    work = tmp_path / "work"
    work.mkdir()
    result = await run_job(
        engine=_Codex(), prompt="go", cwd=work, policy=Policy(allow_write=True),
        approver=DenyAll(), switchboard=True, limits=Limits(max_tool_calls=0),
    )  # fmt: skip
    assert result.error == "limit: 0 tool calls"
    return json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]


async def test_a_call_answered_before_the_engine_said_it_is_recorded_once(tmp_path, monkeypatch):
    [step] = await _cut_off(tmp_path, monkeypatch, "done-first", "gh__list_issues")
    assert step["tool"].endswith("#list_issues") and step["success"] is True


async def test_a_call_cut_off_once_the_server_had_it_may_have_run_and_is_recorded_once(
    tmp_path, monkeypatch
):
    steps = await _cut_off(tmp_path, monkeypatch, "in-flight", "gh__create_issue", "--slow", "30")
    [step] = steps
    assert step["tool"].endswith("#create_issue") and step["output"] == MAY_HAVE_RUN


@pytest.mark.parametrize("attempt", range(3))  # whether it reached the switchboard is a race
async def test_a_call_the_engine_said_before_it_sent_it_is_never_recorded_twice(
    tmp_path, monkeypatch, attempt
):
    steps = await _cut_off(tmp_path, monkeypatch, "print-first", "gh__list_issues")
    assert len(steps) <= 1
    assert all(step["tool"].endswith("#list_issues") for step in steps)


def _steps(result) -> list[dict]:
    return json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]


def _bridge(tmp_path, monkeypatch, *server: str) -> BridgeEngine:
    """A bridge that takes the switchboard, and calls gh__create_issue through it."""
    count = tmp_path / "count"
    ConnectionStore().add(
        name="gh", transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count), *server],
    )  # fmt: skip
    monkeypatch.setenv("FAKE_BRIDGE_TOOL", "gh__create_issue")
    monkeypatch.setenv("FAKE_BRIDGE_ARGS", json.dumps({"title": "t1"}))
    if server:  # a slow server: the bridge says the call once the server has it
        monkeypatch.setenv("FAKE_BRIDGE_COUNT", str(count))
    (tmp_path / "work").mkdir()
    return BridgeEngine(
        "mine", {"command": [sys.executable, str(FAKE_BRIDGE)], "tools": "switchboard"}
    )


async def test_a_bridge_s_switchboard_call_it_reports_is_recorded_once_and_counted(
    tmp_path, monkeypatch
):
    result = await run_job(
        engine=_bridge(tmp_path, monkeypatch), prompt="go", cwd=tmp_path / "work",
        policy=Policy(), approver=DenyAll(), switchboard=True,
    )  # fmt: skip
    [step] = _steps(result)
    assert step["tool"].endswith("#create_issue") and step["success"] is False  # declined
    assert (result.declined, result.failed) == (1, 0)
    meta = json.loads((run_dir(result.run_id) / "journal.json").read_text())["meta"]
    assert meta["tool_calls"] == 1  # it counts toward --max-tool-calls


async def test_a_bridge_s_switchboard_call_cut_off_at_a_limit_is_recorded_once(
    tmp_path, monkeypatch
):
    result = await run_job(
        engine=_bridge(tmp_path, monkeypatch, "--slow", "30"), prompt="go",
        cwd=tmp_path / "work", policy=Policy(allow_write=True), approver=DenyAll(),
        switchboard=True, limits=Limits(max_tool_calls=0),
    )  # fmt: skip
    assert result.error == "limit: 0 tool calls"
    [step] = _steps(result)
    assert step["tool"].endswith("#create_issue") and step["output"] == MAY_HAVE_RUN


class _NeverAnswers:
    """A person at the terminal who never answers."""

    kind = "human"

    def __init__(self) -> None:
        self.asked = asyncio.Event()

    async def ask(self, req) -> bool:
        self.asked.set()
        await asyncio.Event().wait()
        return False


async def test_a_call_stopped_while_a_person_is_asked_is_recorded_declined(tmp_path, monkeypatch):
    ConnectionStore().add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    monkeypatch.setenv("FAKE_CODEX_MODE", "print-first")
    monkeypatch.setenv("FAKE_CODEX_TOOL", "gh__create_issue")
    monkeypatch.setenv("FAKE_CODEX_ARGS", json.dumps({"title": "t1"}))
    (tmp_path / "work").mkdir()
    person = _NeverAnswers()
    run = asyncio.ensure_future(
        run_job(
            engine=_Codex(),
            prompt="go",
            cwd=tmp_path / "work",
            policy=Policy(),
            approver=person,
            switchboard=True,
        )  # fmt: skip
    )
    await asyncio.wait_for(person.asked.wait(), 30)
    run.cancel()
    with pytest.raises(RunCancelled) as stopped:
        await asyncio.wait_for(run, 30)
    result = stopped.value.result
    [step] = _steps(result)  # the tool and its arguments, under the verdict's id
    assert step["tool"].endswith("#create_issue") and step["args"] == {"title": "t1"}
    assert step["output"] == {"error": "declined: cancelled"} and step["success"] is False
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    said = {"result": "warn", "detail": "declined: cancelled"}
    assert {"check": f"scope_approval:{step['call_id']}", **said} in verdicts


def _approvals(result) -> list[tuple[str, str, str]]:
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    return [
        (v["check"][len("scope_approval:") :], v["result"], v["detail"])
        for v in verdicts
        if v["check"].startswith("scope_approval:")
    ]


class _SaysYesLate:
    """A person who says yes a while after they are asked."""

    kind = "human"

    def __init__(self, asked) -> None:
        self.asked = asked

    async def ask(self, req) -> bool:
        self.asked.write_text(req.call_id, encoding="utf-8")
        await asyncio.sleep(1.5)
        return True


async def test_a_call_the_bridge_gives_up_on_while_a_person_is_asked_is_asked_no_longer(
    tmp_path, monkeypatch
):
    asked = tmp_path / "asked"
    monkeypatch.setenv("FAKE_BRIDGE_WITHDRAW", str(asked))
    result = await run_job(
        engine=_bridge(tmp_path, monkeypatch), prompt="go", cwd=tmp_path / "work",
        policy=Policy(), approver=_SaysYesLate(asked), switchboard=True,
    )  # fmt: skip
    [step] = _steps(result)
    assert step["tool"].endswith("#create_issue") and step["success"] is False
    assert step["output"] == {"error": "declined: cancelled"}
    # before: the yes that came after was signed, as an approval of a call never made
    assert _approvals(result) == [(step["call_id"], "warn", "declined: cancelled")]
    assert (result.declined, result.failed) == (1, 0)
    assert not (tmp_path / "count").exists()  # the server never had it


class _SaysYesWhenTold:
    """A person who says yes when the test says so: Allow, clicked just after Stop."""

    kind = "human"

    def __init__(self) -> None:
        self.asked, self.yes = asyncio.Event(), asyncio.Event()

    async def ask(self, req) -> bool:
        self.asked.set()
        await self.yes.wait()
        return True


async def test_a_yes_given_once_the_run_is_stopping_counts_for_nothing(tmp_path, monkeypatch):
    ConnectionStore().add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    monkeypatch.setenv("FAKE_CODEX_MODE", "print-first")
    monkeypatch.setenv("FAKE_CODEX_TOOL", "gh__create_issue")
    monkeypatch.setenv("FAKE_CODEX_ARGS", json.dumps({"title": "t1"}))
    (tmp_path / "work").mkdir()
    person = _SaysYesWhenTold()
    run = asyncio.ensure_future(
        run_job(
            engine=_Codex(),
            prompt="go",
            cwd=tmp_path / "work",
            policy=Policy(),
            approver=person,
            switchboard=True,
        )  # fmt: skip
    )
    await asyncio.wait_for(person.asked.wait(), 30)
    run.cancel()  # Stop
    await asyncio.sleep(0)  # the stop has begun
    person.yes.set()
    with pytest.raises(RunCancelled) as stopped:
        await asyncio.wait_for(run, 30)
    # before: asked until the engine was gone, so the yes was signed as an approval
    [(_, said, why)] = _approvals(stopped.value.result)
    assert (said, why) == ("warn", "declined: cancelled")


@pytest.mark.parametrize("tool", ["gh__create_issues", "skills__load"])
async def test_a_bridge_s_call_to_a_tool_the_switchboard_lacks_is_recorded_failed(
    tmp_path, monkeypatch, tool
):
    engine = _bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_BRIDGE_TOOL", tool)
    result = await run_job(
        engine=engine, prompt="go", cwd=tmp_path / "work", policy=Policy(),
        approver=DenyAll(), switchboard=True,
    )  # fmt: skip
    [step] = _steps(result)  # before: in no record, so no caveat said it failed
    assert step["tool"] == f"metabot/{tool}" and step["success"] is False
    assert step["output"] == {"error": f"no tool named {tool}"}
    assert (result.declined, result.failed) == (0, 1)
