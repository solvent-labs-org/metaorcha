"""Hard limits on a run, a run id given by the caller, and extra meta."""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from fake_chat import answer, call
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.config import Config, load_config, save_config
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.paths import run_dir, run_path
from metabot.receipts import journal_matches, list_index, verify_file
from metabot.runner import Limits, run_job, usage_tokens


def _claude_fixture(tmp_path, monkeypatch, calls: int, usage: dict | None = None) -> None:
    lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "m"}]
    for n in range(1, calls + 1):
        use = {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": f"echo {n}"}}
        done = {"type": "tool_result", "tool_use_id": f"t{n}", "content": str(n), "is_error": False}
        lines.append({"type": "assistant", "message": {"content": [use]}})
        lines.append({"type": "user", "message": {"content": [done]}})
    lines.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s-1",
            "usage": usage or {"input_tokens": 4, "output_tokens": 90},
        }
    )
    path = tmp_path / "fixture.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    monkeypatch.setenv("FAKE_FIXTURE", str(path))


async def _claude_run(tmp_path, **kw):
    return await run_job(
        engine=wrap(ClaudeEngine),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        **kw,
    )


def _meta(result) -> dict:
    return json.loads((run_dir(result.run_id) / "journal.json").read_text())["meta"]


async def test_turn_cap_ends_the_run_with_a_reason(tmp_path, monkeypatch):
    _claude_fixture(tmp_path, monkeypatch, calls=5)
    result = await _claude_run(tmp_path, limits=Limits(max_tool_calls=3))
    assert result.ok is False
    assert result.error == "limit: 3 tool calls"
    assert verify_file(result.receipt).valid and journal_matches(result.run_id)
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["success"] for s in steps] == [True, True, True, False]
    assert steps[-1]["output"] == {"error": "no result"}  # asked for, never answered
    meta = _meta(result)
    assert (meta["limit"], meta["tool_calls"], meta["status"]) == (
        "limit: 3 tool calls",
        4,
        "error",
    )


async def test_past_the_cap_an_engine_with_no_gate_may_have_run_the_call(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-file-and-mcp.jsonl"))
    result = await run_job(
        engine=wrap(CodexEngine),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        limits=Limits(max_tool_calls=1),
    )
    assert result.error == "limit: 1 tool calls" and verify_file(result.receipt).valid
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["tool"] for s in steps] == ["codex/command_execution", "codex/file_change"]
    last = steps[-1]  # its change was made before codex said so: no gate stood before it
    assert last["output"] == {"error": "no result; the engine may have run it"}
    assert last["success"] is False


async def test_a_run_inside_its_limits_is_untouched(tmp_path, monkeypatch):
    _claude_fixture(tmp_path, monkeypatch, calls=3)
    result = await _claude_run(tmp_path, limits=Limits(max_tool_calls=3, max_tokens=1000))
    assert result.ok is True, result.error
    meta = _meta(result)
    assert (meta["tool_calls"], meta["tokens"], meta["limit"]) == (3, 94, None)


async def test_token_budget_reached_ends_the_run(tmp_path, monkeypatch):
    _claude_fixture(tmp_path, monkeypatch, calls=1)
    result = await _claude_run(tmp_path, limits=Limits(max_tokens=50))
    assert result.ok is False
    assert result.error == "limit: 50 tokens"
    assert _meta(result)["tokens"] == 94
    assert verify_file(result.receipt).valid


def _local(fake) -> OpenAIEngine:
    return OpenAIEngine(base_url=fake.url, model="tiny", config=Config())


async def test_the_call_after_the_cap_never_runs_on_the_local_engine(chat, tmp_path):
    script = [call("shell", {"command": f"echo {n} >> ran.txt"}, f"c{n}") for n in range(1, 6)]
    fake = chat(script=[*script, answer("done")])
    result = await run_job(
        engine=_local(fake),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(allow_write=True),
        approver=DenyAll(),
        limits=Limits(max_tool_calls=3),
    )
    assert result.error == "limit: 3 tool calls"
    assert (tmp_path / "ran.txt").read_text().split() == ["1", "2", "3"]


def _with_usage(reply: dict, prompt: int, completion: int) -> dict:
    return {**reply, "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


async def test_the_local_engine_counts_every_turns_tokens(chat, tmp_path):
    script = [
        _with_usage(call("list_dir", {"path": "."}, "c1"), 10, 5),
        _with_usage(call("list_dir", {"path": "."}, "c2"), 10, 5),
        _with_usage(answer("done"), 10, 5),
    ]
    fake = chat(script=script)
    result = await run_job(
        engine=_local(fake), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    assert result.ok is True, result.error
    assert _meta(result)["tokens"] == 45


async def test_a_token_budget_stops_the_local_engine_before_its_next_call(chat, tmp_path):
    script = [
        _with_usage(call("shell", {"command": "echo 1 >> ran.txt"}, "c1"), 10, 5),
        _with_usage(call("shell", {"command": "echo 2 >> ran.txt"}, "c2"), 10, 5),
        _with_usage(answer("done"), 10, 5),
    ]
    fake = chat(script=script)
    result = await run_job(
        engine=_local(fake),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(allow_write=True),
        approver=DenyAll(),
        limits=Limits(max_tokens=20),
    )
    assert result.error == "limit: 20 tokens"
    assert (tmp_path / "ran.txt").read_text().split() == ["1"]


async def test_given_run_id_is_used(tmp_path, monkeypatch):
    _claude_fixture(tmp_path, monkeypatch, calls=0)
    result = await _claude_run(tmp_path, run_id="mb-office.dev")
    assert result.run_id == "mb-office.dev"
    assert result.receipt.name == "mb-office.dev.json"
    assert verify_file(result.receipt).run_id == "mb-office.dev"


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 200])
async def test_an_unsafe_run_id_is_refused_before_anything_is_written(tmp_path, home, bad):
    with pytest.raises(ValueError):
        await _claude_run(tmp_path, run_id=bad)
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())


async def test_a_run_id_in_use_is_refused(tmp_path, monkeypatch):
    _claude_fixture(tmp_path, monkeypatch, calls=0)
    first = await _claude_run(tmp_path, run_id="mb-once")
    before = first.receipt.read_bytes()
    with pytest.raises(ValueError, match="in use"):
        await _claude_run(tmp_path, run_id="mb-once")
    assert first.receipt.read_bytes() == before
    assert run_path("mb-once").is_dir()


async def test_extra_meta_is_kept_and_indexed_but_never_replaces_the_runs_own(
    tmp_path, monkeypatch
):
    _claude_fixture(tmp_path, monkeypatch, calls=0)
    extra = {"office": "mb-o", "desk": "dev", "copy": None, "parent": "mb-o.plan"}
    result = await _claude_run(tmp_path, meta={**extra, "status": "forged", "engine": "x"})
    meta = _meta(result)
    assert {k: meta[k] for k in extra} == extra
    assert (meta["status"], meta["engine"]) == ("ok", "claude")
    row = next(r for r in list_index() if r["run_id"] == result.run_id)
    assert (row["office"], row["desk"], row["parent"]) == ("mb-o", "dev", "mb-o.plan")


@pytest.mark.parametrize(
    ("data", "tokens"),
    [
        ({"usage": {"input_tokens": 4, "output_tokens": 90}}, 94),
        (
            {
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 4,
                }
            },
            10,
        ),
        ({"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}, 120),
        (
            {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "cache_write_input_tokens": 5,
                "output_tokens": 20,
            },
            125,
        ),
        (
            {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 30, "cacheWriteTokens": 1},
            43,
        ),
        ({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, 10),
        ({}, 0),
        ({"input_tokens": "lots", "output_tokens": -5}, 0),
        ({"usage": "none"}, 0),
    ],
)
def test_usage_tokens_counts_the_way_the_office_meter_does(data, tokens):
    assert usage_tokens(data) == tokens


class _AsksAfterTheBudget:
    """An in-process engine whose gate question arrives after the budget is spent."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self) -> None:
        self.reply: dict | None = None

    async def run(self, job, *, board=None, approve):
        from metabot.events import Final, Started, Usage

        yield Started("s-1", None)
        yield Usage({"prompt_tokens": 100, "completion_tokens": 0})
        question = {"op": "approve", "call_id": "c1", "tool": "fake/Edit", "cls": "write"}
        self.reply = await approve({**question, "preview": "{}"})
        yield Final("done", "s-1")


async def test_past_a_limit_the_gate_allows_nothing(tmp_path):
    engine = _AsksAfterTheBudget()
    result = await run_job(
        engine=engine,
        prompt="go",
        cwd=tmp_path,
        policy=Policy(allow_write=True),
        approver=DenyAll(),
        limits=Limits(max_tokens=10),
    )
    assert engine.reply == {"allow": False, "reason": "limit: 10 tokens"}
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    assert {
        "check": "scope_approval:c1",
        "result": "warn",
        "detail": "declined: limit: 10 tokens",
    } in [{k: v[k] for k in ("check", "result", "detail")} for v in verdicts]
    assert result.final_text == "done" and result.error == "limit: 10 tokens"


async def test_a_run_folder_left_without_a_receipt_still_holds_its_id(tmp_path, home):
    (home / "runs" / "mb-crashed").mkdir(parents=True)
    with pytest.raises(ValueError, match="in use"):
        await _claude_run(tmp_path, run_id="mb-crashed")


# What a run spent, and a cap on what it may spend.


def _priced(fake, prices: dict) -> OpenAIEngine:
    save_config(Config(prices=prices))
    return OpenAIEngine(base_url=fake.url, model="tiny", config=load_config())


def _verdict(result, check: str) -> dict:
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    return next(v for v in verdicts if v["check"] == check)


TINY = {"tiny": {"in": 1.0, "out": 2.0}}  # 20 micro-dollars a turn below


async def test_a_money_cap_stops_the_local_engine_before_its_next_call(chat, tmp_path):
    script = [
        _with_usage(call("shell", {"command": "echo 1 >> ran.txt"}, "c1"), 10, 5),
        _with_usage(call("shell", {"command": "echo 2 >> ran.txt"}, "c2"), 10, 5),
        _with_usage(answer("done"), 10, 5),
    ]
    result = await run_job(
        engine=_priced(chat(script=script), TINY),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(allow_write=True),
        approver=DenyAll(),
        limits=Limits(max_cost_musd=30),
    )
    assert result.error == "limit: $0.00003"
    assert (tmp_path / "ran.txt").read_text().split() == ["1"]
    assert _verdict(result, "budget") == {
        "check": "budget",
        "result": "fail",
        "detail": "cap_musd=30;spent_musd=40",
    }
    assert verify_file(result.receipt).valid


async def test_a_run_inside_its_money_cap_says_so(chat, tmp_path):
    script = [_with_usage(answer("done"), 10, 5)]
    result = await run_job(
        engine=_priced(chat(script=script), TINY),
        prompt="go",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        limits=Limits(max_cost_musd=30),
    )
    assert result.ok, result.error
    assert _verdict(result, "budget")["result"] == "pass"
    meta = _meta(result)
    assert (meta["cost_musd"], meta["cost_source"]) == (20, "config")
    assert meta["spend"] == {
        "input": 10,
        "output": 5,
        "cache_read": 0,
        "cache_write": 0,
        "reported_musd": None,
    }


async def test_a_local_run_costs_nothing_and_its_receipt_says_why(chat, tmp_path):
    fake = chat(script=[_with_usage(answer("done"), 10, 5)])
    result = await run_job(
        engine=_local(fake), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    meta = _meta(result)
    assert (meta["cost_musd"], meta["cost_source"]) == (0, "local")
    usage = _verdict(result, "usage")
    assert usage["result"] == "pass"
    assert usage["detail"].startswith("in=10;out=5;cache_read=0;cache_write=0;cost_musd=0;")
    assert ";source=local;prices=" in usage["detail"]
    assert "budget" not in [v["check"] for v in json.loads(result.receipt.read_text())["verdicts"]]


async def test_a_plan_run_is_not_said_to_cost_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-echo.jsonl"))
    result = await run_job(
        engine=wrap(CodexEngine), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    meta = _meta(result)
    assert (meta["cost_musd"], meta["cost_source"]) == (None, "plan")
    assert meta["tokens"] == 32268 + 82
    assert ";cost_musd=none;source=plan;" in _verdict(result, "usage")["detail"]
    row = next(r for r in list_index() if r["run_id"] == result.run_id)
    assert (row["cost_musd"], row["cost_source"]) == (None, "plan")


async def test_a_money_cap_with_no_price_refuses_before_the_run(tmp_path, home):
    with pytest.raises(ValueError, match="no price for codex/gpt-5"):
        await run_job(
            engine=wrap(CodexEngine),
            prompt="go",
            cwd=tmp_path,
            model="gpt-5",
            policy=Policy(),
            approver=DenyAll(),
            limits=Limits(max_cost_musd=1_000_000),
        )
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())


async def test_claudes_reported_cost_is_signed_and_cannot_be_edited(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _claude_run(tmp_path)
    meta = _meta(result)
    assert (meta["cost_musd"], meta["cost_source"]) == (1695, "engine")
    usage = _verdict(result, "usage")
    assert ";cost_musd=1695;source=engine;" in usage["detail"]
    envelope = json.loads(result.receipt.read_text())
    for v in envelope["verdicts"]:
        if v["check"] == "usage":
            v["detail"] = v["detail"].replace("cost_musd=1695", "cost_musd=1")
    result.receipt.write_text(json.dumps(envelope))
    assert not verify_file(result.receipt).valid


def _claude_messages(tmp_path, monkeypatch, per_message: list[int], total: dict) -> None:
    """claude, one message a call: each with its own usage, then the run's total."""
    lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-opus-5-5"}]
    for n, out in enumerate(per_message, start=1):
        use = {"type": "tool_use", "id": f"t{n}", "name": "Read", "input": {"file_path": "x"}}
        usage = {"input_tokens": 0, "output_tokens": out}
        for block in ({"type": "text", "text": "reading"}, use):  # one line a block, one message
            message = {"id": f"msg_{n}", "content": [block], "usage": usage}
            lines.append({"type": "assistant", "message": message})
        done = {"type": "tool_result", "tool_use_id": f"t{n}", "content": "x", "is_error": False}
        lines.append({"type": "user", "message": {"content": [done]}})
    lines.append(
        {"type": "result", "subtype": "success", "is_error": False, "result": "done",
         "session_id": "s-1", **total}
    )  # fmt: skip
    path = tmp_path / "fixture.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    monkeypatch.setenv("FAKE_FIXTURE", str(path))


async def test_claude_counts_each_message_once_then_takes_the_result(tmp_path, monkeypatch):
    total = {"usage": {"input_tokens": 4, "output_tokens": 90}, "total_cost_usd": 0.002}
    _claude_messages(tmp_path, monkeypatch, [10, 20], total)
    result = await _claude_run(tmp_path)
    meta = _meta(result)
    assert (meta["tokens"], meta["cost_musd"], meta["cost_source"]) == (94, 2000, "engine")


async def test_a_money_cap_stops_claude_on_its_running_count(tmp_path, monkeypatch):
    # opus 5.5 output is $20 a million: 20 micro-dollars a token
    total = {"usage": {"input_tokens": 0, "output_tokens": 300}, "total_cost_usd": 0.006}
    _claude_messages(tmp_path, monkeypatch, [100, 100, 100], total)
    result = await _claude_run(tmp_path, limits=Limits(max_cost_musd=3_000))
    assert result.error == "limit: $0.003"
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["success"] for s in steps] == [True, False]  # the call past the cap is not answered
