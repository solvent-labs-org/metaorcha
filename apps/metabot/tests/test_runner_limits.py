"""Hard limits on a run, a run id given by the caller, and extra meta."""

from __future__ import annotations

import dataclasses
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
from metabot.prices import rate_for
from metabot.receipts import journal_matches, list_index, verify_file
from metabot.runner import Limits, run_job, usage_tokens
from metabot.spend import Spend
from test_office_run import DeskEngine


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


async def test_a_cli_engine_s_switchboard_calls_are_the_switchboard_s_to_record(
    tmp_path, monkeypatch
):
    """Past the cap as before it: the switchboard records a call it took, once; the run
    records none of them, so none is in the record twice."""
    lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": "m"}]
    for n in (1, 2):
        use = {"type": "tool_use", "id": f"t{n}", "name": "mcp__metabot__gh__list_issues",
               "input": {}}  # fmt: skip
        lines.append({"type": "assistant", "message": {"content": [use]}})
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    result = await _claude_run(tmp_path, switchboard=True, limits=Limits(max_tool_calls=1))
    assert result.error == "limit: 1 tool calls"
    assert json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"] == []


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


class _AsksAfterALostTurn(_AsksAfterTheBudget):
    """Its gate question comes after a turn that never said what it spent, before the run
    hears of the call, as a CLI engine's switchboard or hook asks."""

    async def run(self, job, *, board=None, approve):
        from metabot.events import Final, Started, Usage

        yield Started("s-1", job.model)
        yield Usage({}, lost=True)
        question = {"op": "approve", "call_id": "c1", "tool": "fake/Edit", "cls": "write"}
        self.reply = await approve({**question, "preview": "{}"})
        yield Final("done", "s-1")


async def test_a_call_asked_before_the_run_hears_of_it_meets_the_money_cap(tmp_path):
    save_config(Config(prices=QUIET))
    engine = _AsksAfterALostTurn()
    result = await run_job(
        engine=engine, prompt="go", cwd=tmp_path, policy=Policy(allow_write=True),
        approver=DenyAll(), model="m-quiet", limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert engine.reply is not None and engine.reply["allow"] is False
    why = engine.reply["reason"]
    assert why.endswith(": a request that went out never said what it spent"), why
    assert not result.ok and result.error == why
    assert _verdict(result, "scope_approval:c1")["detail"] == f"declined: {why}"


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


def _claude_messages(
    tmp_path,
    monkeypatch,
    per_message: list[int],
    total: dict,
    named: str = "claude-opus-5-5",
    ran: str | None = None,
) -> None:
    """claude, one message a call: each with its own usage, then the run's total.

    ``named``: the model claude names as it starts; ``ran``: the one each message names.
    """
    lines = [{"type": "system", "subtype": "init", "session_id": "s-1", "model": named}]
    for n, out in enumerate(per_message, start=1):
        use = {"type": "tool_use", "id": f"t{n}", "name": "Read", "input": {"file_path": "x"}}
        usage = {"input_tokens": 0, "output_tokens": out}
        for block in ({"type": "text", "text": "reading"}, use):  # one line a block, one message
            message = {"id": f"msg_{n}", "content": [block], "usage": usage}
            lines.append(
                {"type": "assistant", "message": {**message, **({"model": ran} if ran else {})}}
            )
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


# ── claude may run its tokens on a model other than the one it names ─────────

# In plan mode, which a read-only run uses, claude names haiku as it starts and sends each
# request to sonnet: $15 a million output tokens, where haiku's are $5.
PLAN_MODE = {"named": "claude-haiku-4-5-20251001", "ran": "claude-sonnet-4-6"}


def _model_verdict(result) -> str:
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    return next(v["detail"] for v in verdicts if v["check"] == "model")


async def test_each_claude_message_is_priced_by_the_model_that_ran_it(tmp_path, monkeypatch):
    total = {"usage": {"input_tokens": 0, "output_tokens": 3000}, "total_cost_usd": 0.045}
    _claude_messages(tmp_path, monkeypatch, [1000, 1000, 1000], total, **PLAN_MODE)
    result = await _claude_run(
        tmp_path, model="haiku", read_only=True, limits=Limits(max_cost_musd=10_000)
    )
    assert result.error == "limit: $0.01"
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["success"] for s in steps] == [False]  # sonnet's first message is past the cap
    assert (result.cost_musd, result.cost_source) == (15_000, "table")
    assert _model_verdict(result) == "claude/claude-haiku-4-5-20251001;ran_on=claude-sonnet-4-6"


async def test_a_total_with_no_cost_is_never_under_what_each_message_was_priced_at(
    tmp_path, monkeypatch
):
    total = {"usage": {"input_tokens": 0, "output_tokens": 3000}}  # and no total_cost_usd
    _claude_messages(tmp_path, monkeypatch, [1000, 1000, 1000], total, **PLAN_MODE)
    result = await _claude_run(tmp_path, model="haiku", read_only=True)
    assert result.ok, result.error
    assert (result.cost_musd, result.cost_source) == (45_000, "table")


# haiku 5.5 is $0.10 and $0.50 a million, and $0.50 and $2.50 for a request whose prompt is
# past 100k tokens: a request of 15k in and 100 out costs 1,550 micro-dollars.
REQUEST = {"input_tokens": 15_000, "output_tokens": 100}
TEN_REQUESTS = {"input_tokens": 150_000, "output_tokens": 1_000}


class _Requests(DeskEngine):
    """An in-process engine that reports its requests' usage, then the run's total."""

    def __init__(self, reports: int) -> None:
        super().__init__("claude")
        self.reports = reports

    async def run(self, job, *, board=None, approve):
        from metabot.events import Final, Started, Usage

        yield Started("s-t", job.model)
        for _ in range(self.reports):
            yield Usage(dict(REQUEST))
        yield Usage(dict(TEN_REQUESTS), total=True)
        yield Final("done", "s-t")


@pytest.mark.parametrize(
    ("reports", "cost"),
    [(10, 15_500), (5, 15_500), (0, 77_500)],  # what it cannot split, it prices as one
    ids=["each-request", "some-requests", "a-total-alone"],
)
async def test_a_run_s_total_is_priced_by_the_requests_it_reported_not_as_one(
    tmp_path, reports, cost
):
    result = await run_job(
        engine=_Requests(reports), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), model="claude-haiku-5-5", limits=Limits(max_cost_musd=50_000),
    )  # fmt: skip
    assert (result.cost_musd, result.cost_source) == (cost, "table")  # before: 77,500 in each
    assert result.ok == (cost <= 50_000), result.error


class _Says(DeskEngine):
    """An in-process engine that says these events, in this order, then answers."""

    def __init__(self, *events, name: str = "claude", model: str | None = None) -> None:
        super().__init__(name)
        self.events = events
        self.model = model  # the one it runs when none is asked for, as a bridge's own

    async def run(self, job, *, board=None, approve):
        from metabot.events import Final

        for event in self.events:
            yield event
        yield Final("done", "s-t")


CACHED = {"prompt_tokens": 15_000, "completion_tokens": 100}
CACHED["prompt_tokens_details"] = {"cached_tokens": 14_000}  # as OpenAI's API says it
SUMMED = {"prompt_tokens": 150_000, "completion_tokens": 1_000}  # the same, details left out


@pytest.mark.parametrize(
    ("model", "cost"), [("claude-haiku-5-5", 2_900), ("claude-sonnet-4-6", 87_000)]
)
async def test_a_total_that_splits_the_same_tokens_another_way_adds_nothing(tmp_path, model, cost):
    from metabot.events import Started, Usage

    requests = [Usage({"usage": dict(CACHED)}) for _ in range(10)]
    engine = _Says(Started("s-t", model), *requests, Usage({"usage": dict(SUMMED)}, total=True))
    result = await run_job(
        engine=engine, prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
        model=model, limits=Limits(max_cost_musd=100_000),
    )  # fmt: skip
    # before: the 140k read from the cache were charged again as input, 72,900 and 507,000
    assert (result.cost_musd, result.cost_source) == (cost, "table")
    assert result.ok, result.error


@pytest.mark.parametrize("total", [False, True], ids=["requests", "and-a-total"])
@pytest.mark.parametrize("named", ["after-its-requests", "again"])
async def test_tokens_counted_before_a_model_with_a_price_is_named_have_no_cost(
    tmp_path, named, total
):
    from metabot.events import Started, Usage

    alias = "my-gateway-alias"  # a name with no price
    events = [Usage({"usage": dict(REQUEST)}) for _ in range(10)]
    events.append(Started("s-t", "claude-haiku-5-5"))  # the model it ran on, said late
    if named == "again":
        events.insert(0, Started("s-t", alias))
    if total:
        events.append(Usage({"usage": dict(TEN_REQUESTS)}, total=True))
    result = await run_job(
        engine=_Says(*events), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
        model=alias,
    )  # fmt: skip
    assert result.tokens == 151_000
    # before: $0.00, by the listed rates, for tokens nothing priced
    assert (result.cost_musd, result.cost_source) == (None, "unknown")


def _signed(result) -> tuple[Spend, int | None]:
    """The tokens of each kind and the cost the receipt's usage verdict signs."""
    said = dict(field.split("=", 1) for field in _verdict(result, "usage")["detail"].split(";"))
    kinds = Spend(*(int(said[k]) for k in ("in", "out", "cache_read", "cache_write")))
    return kinds, None if said["cost_musd"] == "none" else int(said["cost_musd"])


OPENAI_200K = {"prompt_tokens": 200_000, "completion_tokens": 1_000}  # no key for a cache write
WRITES_199K = {
    "input_tokens": 1_000,
    "cache_creation_input_tokens": 199_000,
    "output_tokens": 1_000,
}


@pytest.mark.parametrize(
    ("model", "requests", "total", "kinds"),
    [
        # the total leaves out what was read from the cache
        ("claude-haiku-5-5", [CACHED] * 10, SUMMED, Spend(10_000, 1_000, 140_000)),
        ("claude-haiku-5-5", [CACHED] * 10, {**SUMMED, "prompt_tokens": 160_000},
         Spend(20_000, 1_000, 140_000)),  # and holds 10k more
        # the total puts what the requests read in full under writes to the cache
        ("claude-sonnet-4-6", [OPENAI_200K], WRITES_199K, Spend(200_000, 1_000)),
        # the total holds fewer than the requests said
        ("claude-haiku-5-5", [REQUEST] * 10, {"input_tokens": 1_000, "output_tokens": 10},
         Spend(150_000, 1_000)),
    ],
    ids=["cache-left-out", "and-more", "moved-to-writes", "fewer"],
)  # fmt: skip
async def test_the_receipt_signs_the_tokens_as_they_were_priced(
    tmp_path, model, requests, total, kinds
):
    from metabot.events import Started, Usage

    said = [Usage({"usage": dict(r)}) for r in requests] + [Usage({"usage": total}, total=True)]
    result = await run_job(
        engine=_Says(Started("s-t", model), *said), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), model=model,
    )  # fmt: skip
    signed, cost = _signed(result)
    # before: the total's kinds, whose price could be past the cost signed beside them, or
    # fewer tokens than the run spent
    assert signed == kinds and result.tokens == kinds.tokens
    rate = rate_for(model, {})
    assert rate is not None and cost is not None
    assert dataclasses.replace(rate, above=None).cost(signed) <= cost


@pytest.mark.parametrize("asked", ["by -m", "by the engine"])
async def test_tokens_reported_before_the_engine_names_another_model_have_no_cost(tmp_path, asked):
    from metabot.events import Started, ToolCall, Usage

    events = [Usage({"usage": dict(REQUEST)}) for _ in range(10)]  # haiku 4.5 is asked for
    events += [Started("s-t", "claude-opus-5"), ToolCall("c1", "Read", {"path": "."})]
    own = None if asked == "by -m" else "claude-haiku-4-5"
    result = await run_job(
        engine=_Says(*events, name="probe", model=own), prompt="go", cwd=tmp_path,
        policy=Policy(), approver=DenyAll(), model=None if own else "claude-haiku-4-5",
        limits=Limits(max_cost_musd=500_000),
    )  # fmt: skip
    # before: $0.155 by haiku 4.5's rates, for a run its receipt says ran on opus 5, whose
    # rates put it past the cap
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    assert result.error == "limit: $0.50: probe reported tokens before it named its model"


async def test_the_cost_line_says_the_tokens_came_before_the_model_was_named(tmp_path):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    events = [Usage({"usage": dict(REQUEST)}) for _ in range(10)]
    events.append(Started("s-t", "claude-haiku-5-5"))  # a model with a price, named late
    result = await run_job(
        engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(),
    )  # fmt: skip
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    # before: "no price for this model: add one under prices", which a price never changed
    assert cost_line(result, "probe") == (
        "[metabot] 151000 tokens, some reported before probe named its model, so there is no "
        "cost to show"
    )


def _named_late(*early: dict) -> list:
    """Usage reports of ``early``, then a start naming sonnet 4.6, then a call."""
    from metabot.events import Started, ToolCall, Usage

    events = [Usage({"usage": dict(u)}) for u in early]
    return [*events, Started("s-t", "claude-sonnet-4-6"), ToolCall("c1", "Read", {"path": "."})]


async def test_tokens_that_cost_the_same_by_the_model_then_named_keep_their_cost(tmp_path):
    # a price in config.json with no cache rates reads and writes the cache at its input
    # rate, which is not sonnet 4.6's; tokens that used no cache cost the same by either
    save_config(Config(prices={"my-sonnet": {"in": 3.0, "out": 15.0}}))
    events = _named_late({"input_tokens": 1_000, "output_tokens": 100})
    result = await run_job(
        engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), model="my-sonnet", limits=Limits(max_cost_musd=500_000),
    )  # fmt: skip
    # before: "limit: $0.50: probe reported tokens before it named its model", and no cost
    assert result.ok, result.error
    assert (result.cost_musd, result.cost_source) == (4_500, "table")
    assert result.named_late is False


async def test_more_early_reports_than_are_kept_are_priced_by_the_rates_alone(
    tmp_path, monkeypatch
):
    from metabot import runner

    monkeypatch.setattr(runner, "MAX_EARLY", 2)
    save_config(Config(prices={"my-sonnet": {"in": 3.0, "out": 15.0}}))
    events = _named_late(*[{"input_tokens": 1_000, "output_tokens": 100}] * 3)
    result = await run_job(
        engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), model="my-sonnet",
    )  # fmt: skip
    # the third is not kept, so what it would cost by sonnet 4.6's rates is not known
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    assert result.named_late is True


async def test_tokens_counted_at_no_price_stay_unknown_when_a_priced_model_is_named_next(
    tmp_path,
):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    events = [Usage({"usage": {"input_tokens": 100_000, "output_tokens": 10_000}})]
    events += [Started("s-t", "my-gateway-alias"), Started("s-t", "claude-opus-5")]
    events.append(Usage({"usage": {"input_tokens": 10, "output_tokens": 1}}))
    result = await run_job(
        engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), model="my-gateway-alias",
    )  # fmt: skip
    # before: $0.000075 by opus 5's rates, signed as known, for 110,011 tokens of which
    # 110,000 were counted at no price
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    # before: "no price for this model", read as opus 5, which has one; then that some came
    # before the model was named, when a price for the one asked for is what counts them
    assert cost_line(result, "probe") == (
        "[metabot] 110011 tokens; no price for my-gateway-alias: add one under prices"
    )


@pytest.mark.parametrize("first", ["usage", "started"])
async def test_tokens_on_a_model_with_no_price_ask_for_its_price_whatever_came_first(
    tmp_path, first
):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    usage = [Usage({"usage": {"input_tokens": 100_000, "output_tokens": 10_000}})]
    start = [Started("s-t", "m-1")]  # the model asked for, which has no price
    events = [*usage, *start] if first == "usage" else [*start, *usage]
    events.append(Usage({"usage": {"input_tokens": 10, "output_tokens": 1}}))

    async def run():
        return await run_job(
            engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
            approver=DenyAll(), model="m-1",
        )  # fmt: skip

    result = await run()
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    # before: when the tokens came first, that some came before probe named its model, as if
    # their order kept the cost unknown; and "this model" for the rest
    assert cost_line(result, "probe") == (
        "[metabot] 110011 tokens; no price for m-1: add one under prices"
    )
    save_config(Config(prices={"m-1": {"in": 1.0, "out": 2.0}}))
    result = await run()
    assert (result.cost_musd, result.cost_source) == (120_012, "config")


@pytest.mark.parametrize("asked", ["by -m", "by the engine"])
async def test_tokens_on_models_it_names_ask_for_the_price_that_counts_them_all(tmp_path, asked):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    events = [Started("s-t", "m-2")]
    events.append(Usage({"usage": {"input_tokens": 100_000, "output_tokens": 10_000}}))
    events += [Started("s-t", "m-3"), Usage({"usage": {"input_tokens": 10, "output_tokens": 1}})]
    own = None if asked == "by -m" else "m-1"  # m-1 is asked for; no model has a price

    async def run():
        return await run_job(
            engine=_Says(*events, name="probe", model=own), prompt="go", cwd=tmp_path,
            policy=Policy(), approver=DenyAll(), model=None if own else "m-1",
        )  # fmt: skip

    result = await run()
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    # before: "no price for m-2", and with a price for it, "no price for m-3"; each model it
    # names that has no price of its own is priced as the one asked for
    assert cost_line(result, "probe") == (
        "[metabot] 110011 tokens; no price for m-1: add one under prices"
    )
    save_config(Config(prices={"m-1": {"in": 1.0, "out": 2.0}}))
    result = await run()
    assert (result.cost_musd, result.cost_source) == (120_012, "config")


@pytest.mark.parametrize("asked", [None, "m-1"])
async def test_a_run_whose_last_named_model_has_no_price_names_the_one_to_price(tmp_path, asked):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    events = [Started("s-t", "m-2")]
    events.append(Usage({"usage": {"input_tokens": 100_000, "output_tokens": 10_000}}))
    events.append(Started("s-t", "m-3"))  # no price; nor has m-1, asked for
    m2 = {"m-2": {"in": 1.0, "out": 2.0}}

    async def run():
        return await run_job(
            engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
            approver=DenyAll(), model=asked,
        )  # fmt: skip

    save_config(Config(prices=m2))
    result = await run()
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    # before: "no price for this model", and ask --json had none to name
    assert cost_line(result, "probe") == (
        f"[metabot] 110000 tokens; no price for {asked or 'm-3'}: add one under prices"
    )
    assert result.unpriced == [asked or "m-3"]
    save_config(Config(prices={**m2, asked or "m-3": {"in": 9.0, "out": 9.0}}))
    result = await run()
    assert (result.cost_musd, result.cost_source) == (120_000, "config")


async def test_a_run_on_two_models_with_no_price_names_both_under_a_cap(tmp_path, monkeypatch):
    from metabot.commands import cost_line

    total = {"usage": {"input_tokens": 0, "output_tokens": 300}}
    _claude_messages(
        tmp_path, monkeypatch, [100], total, named="claude-haiku-9", ran="claude-sonnet-9"
    )
    cap = Limits(max_cost_musd=3_000)
    result = await _claude_run(tmp_path, model="haiku", limits=cap)
    # before: "no price for claude/claude-sonnet-9", and with a price for it, the next run
    # stopped at the same call with "no price for claude/claude-haiku-9"
    named = "claude/claude-sonnet-9 and claude/haiku"
    assert result.error == f"limit: $0.003: no price for {named} to count it by"
    assert result.unpriced == ["claude-sonnet-9", "haiku"]
    assert cost_line(result, "claude").endswith(
        "no price for claude-sonnet-9 and haiku: add one for each under prices"
    )
    rate = {"in": 1.0, "out": 2.0}
    save_config(Config(prices={"claude-sonnet-9": rate, "haiku": rate}))
    result = await _claude_run(tmp_path, model="haiku", limits=cap)
    assert result.limit is None and result.cost_source == "config"


@pytest.mark.parametrize("cap", [None, 1_000_000])
async def test_a_run_whose_reports_ran_on_two_models_with_no_price_names_both(tmp_path, cap):
    from metabot.commands import cost_line
    from metabot.events import Started, ToolCall, Usage

    use = {"usage": {"input_tokens": 0, "output_tokens": 300}}
    events = [Started("s-t", "m-1"), Usage(use, model="x-1"), Usage(use, model="x-2")]
    events.append(ToolCall("c1", "Read", {"path": "."}))  # as two subagents on their own models
    rate = {"in": 1.0, "out": 2.0}

    async def run():
        return await run_job(
            engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
            approver=DenyAll(), model="m-1", limits=Limits(max_cost_musd=cap),
        )  # fmt: skip

    save_config(Config(prices={"m-1": rate}))
    result = await run()
    # before: x-1 alone, and with a price for it the next run named x-2
    assert result.unpriced == ["x-1", "x-2"]
    assert result.limit == (
        cap and "limit: $1.00: no price for probe/x-1 and probe/x-2 to count it by"
    )
    assert cost_line(result, "probe").endswith(
        "no price for x-1 and x-2: add one for each under prices"
    )
    save_config(Config(prices={"m-1": rate, "x-1": rate, "x-2": rate}))
    result = await run()
    assert result.limit is None and result.cost_source == "config"


@pytest.mark.parametrize("ran", ["claude-haiku-9", "claude-haiku-9-20261001"])
async def test_a_run_on_its_own_model_by_another_name_names_the_one_price(
    tmp_path, monkeypatch, ran
):
    from metabot.commands import cost_line

    total = {"usage": {"input_tokens": 0, "output_tokens": 300}}
    _claude_messages(tmp_path, monkeypatch, [100], total, named="claude-haiku-9", ran=ran)
    cap = Limits(max_cost_musd=3_000)
    result = await _claude_run(tmp_path, model="haiku", limits=cap)
    # before: "claude/claude-haiku-9 and claude/haiku", one model, either price enough
    assert result.error == "limit: $0.003: no price for claude/haiku to count it by"
    assert result.unpriced == ["haiku"]
    assert cost_line(result, "claude").endswith("no price for haiku: add one under prices")
    save_config(Config(prices={"haiku": {"in": 1.0, "out": 2.0}}))
    result = await _claude_run(tmp_path, model="haiku", limits=cap)
    assert result.limit is None and result.cost_source == "config"


async def test_tokens_before_a_model_is_named_with_none_asked_for_ask_for_no_price(tmp_path):
    from metabot.commands import cost_line
    from metabot.events import Started, Usage

    events = [Usage({"usage": {"input_tokens": 100_000, "output_tokens": 10_000}})]
    events += [Started("s-t", "m-1"), Usage({"usage": {"input_tokens": 10, "output_tokens": 1}})]

    async def run():
        return await run_job(
            engine=_Says(*events, name="probe"), prompt="go", cwd=tmp_path, policy=Policy(),
            approver=DenyAll(),
        )  # fmt: skip

    said = "[metabot] 110011 tokens, some reported before probe named its model, so there is "
    for prices in ({}, {"m-1": {"in": 1.0, "out": 2.0}}):  # no model priced the first ones
        save_config(Config(prices=prices))
        result = await run()
        assert (result.cost_musd, result.cost_source) == (None, "unknown")
        # before: "no price for m-1: add one under prices", and a price for it did not count
        # the tokens reported before it was named
        assert cost_line(result, "probe") == said + "no cost to show"


@pytest.mark.parametrize("asked", [None, "claude-haiku-4-5"])  # one with a price of its own
async def test_a_message_on_a_model_with_no_price_leaves_the_cost_unknown(
    tmp_path, monkeypatch, asked
):
    total = {"usage": {"input_tokens": 0, "output_tokens": 300}}
    named = {"named": "claude-haiku-4-5-20251001"} if asked else {}
    _claude_messages(tmp_path, monkeypatch, [100, 100, 100], total, ran="claude-opus-9", **named)
    result = await _claude_run(tmp_path, model=asked, limits=Limits(max_cost_musd=3_000))
    assert result.error == "limit: $0.003: no price for claude/claude-opus-9 to count it by"
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["success"] for s in steps] == [False]
    assert (result.cost_musd, result.cost_source) == (None, "unknown")


async def test_a_message_with_no_tokens_names_no_model_that_ran(tmp_path, monkeypatch):
    total = {"usage": {"input_tokens": 0, "output_tokens": 300}, "total_cost_usd": 0.006}
    _claude_messages(tmp_path, monkeypatch, [100, 100, 100], total, ran="claude-opus-5-5")
    fixture = tmp_path / "fixture.jsonl"
    empty = {"input_tokens": 0, "output_tokens": 0}
    said = {"id": "msg_s", "model": "<synthetic>", "content": [], "usage": empty}
    first, *rest = fixture.read_text().splitlines(keepends=True)
    fixture.write_text(
        "".join([first, json.dumps({"type": "assistant", "message": said}) + "\n", *rest])
    )
    result = await _claude_run(tmp_path, limits=Limits(max_cost_musd=3_000))
    assert result.error == "limit: $0.003"  # counted at opus 5.5's rate, as it ran
    assert _model_verdict(result) == "claude/claude-opus-5-5"


# The model asked for, as the API, a cloud or claude names it: a snapshot, a cloud's id
# for it, or its [1m]. Its messages are priced as the model asked for.
OWN_WAY = {
    "cloud": ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
    "dated-at": ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5-20250929"),
    "1m": ("claude-sonnet-4-5[1m]", "claude-sonnet-4-5-20250929"),
    "latest": ("claude-3-7-sonnet-latest", "claude-3-7-sonnet-20250219"),
}


@pytest.mark.parametrize(("asked", "ran"), list(OWN_WAY.values()), ids=list(OWN_WAY))
async def test_a_message_on_the_model_asked_for_under_another_name_is_priced_as_asked(
    tmp_path, monkeypatch, asked, ran
):
    save_config(Config(prices={asked: {"in": 3.0, "out": 15.0}}))  # $15 a million out
    total = {"usage": {"input_tokens": 0, "output_tokens": 300}}  # and no total_cost_usd
    _claude_messages(tmp_path, monkeypatch, [100, 100, 100], total, named=asked, ran=ran)
    capped = await _claude_run(tmp_path, model=asked, limits=Limits(max_cost_musd=1_000_000))
    assert capped.ok, capped.error
    assert (capped.cost_musd, capped.cost_source) == (4_500, "config")
    cut = await _claude_run(tmp_path, model=asked, limits=Limits(max_tool_calls=1))
    assert cut.error == "limit: 1 tool calls"
    assert (cut.cost_musd, cut.cost_source) == (3_000, "config")  # two messages had come


async def test_a_last_claude_message_on_a_model_with_no_price_is_kept_under_a_cap(
    tmp_path, monkeypatch
):
    total = {"usage": {"input_tokens": 0, "output_tokens": 200}, "total_cost_usd": 0.002}
    _claude_messages(tmp_path, monkeypatch, [100], total)
    fixture = tmp_path / "fixture.jsonl"
    *lines, ended = fixture.read_text().splitlines(keepends=True)
    usage = {"input_tokens": 0, "output_tokens": 100}
    text = [{"type": "text", "text": "done"}]
    said = {"id": "msg_last", "model": "claude-opus-9", "content": text, "usage": usage}
    lines.append(json.dumps({"type": "assistant", "message": said}) + "\n")
    fixture.write_text("".join([*lines, ended]))
    result = await _claude_run(tmp_path, limits=Limits(max_cost_musd=1_000_000))
    assert result.ok, result.error  # no call came after it for the cap to stop
    assert (result.cost_musd, result.cost_source) == (2_000, "engine")


@pytest.mark.parametrize(
    ("named", "ran"),
    [
        ("claude-opus-4-8@20260101", "claude-opus-4-8"),
        ("us.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8-20260101"),
    ],
)
async def test_a_message_on_the_model_asked_for_takes_its_own_names_price_when_that_has_one(
    tmp_path, monkeypatch, named, ran
):
    total = {"usage": {"input_tokens": 0, "output_tokens": 100_000}}  # and no total_cost_usd
    _claude_messages(tmp_path, monkeypatch, [], total, named=named)
    fixture = tmp_path / "fixture.jsonl"
    *lines, ended = fixture.read_text().splitlines(keepends=True)
    usage = {"input_tokens": 0, "output_tokens": 100_000}
    text = [{"type": "text", "text": "done"}]
    said = {"id": "msg_1", "model": ran, "content": text, "usage": usage}
    lines.append(json.dumps({"type": "assistant", "message": said}) + "\n")
    fixture.write_text("".join([*lines, ended]))
    result = await _claude_run(tmp_path, limits=Limits(max_cost_musd=1_000_000))
    assert result.error == "limit: $1.00"  # $2.50 at its own name's price: never $0


# ── a priced run whose usage MetaBot cannot count ───────────────────────────

QUIET = {"m-quiet": {"in": 1.0, "out": 2.0}}


class _Reports(DeskEngine):
    """An in-process engine that answers, sending ``usage`` first when it has one."""

    def __init__(self, usage: dict | None = None) -> None:
        super().__init__("claude")
        self.usage = usage

    async def run(self, job, *, board=None, approve):
        from metabot.events import Final, Started, Usage

        yield Started("s-q", job.model)
        if self.usage is not None:
            yield Usage(self.usage)
        yield Final("done", "s-q")


async def _quiet_run(tmp_path, usage: dict | None):
    save_config(Config(prices=QUIET))
    return await run_job(
        engine=_Reports(usage), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
        model="m-quiet", limits=Limits(max_cost_musd=30),
    )  # fmt: skip


async def test_a_priced_run_that_reports_no_usage_has_no_cost_and_its_cap_cannot_hold(tmp_path):
    from metabot.commands import cost_line

    result = await _quiet_run(tmp_path, None)
    assert result.ok, result.error
    assert (result.cost_musd, result.cost_source) == (None, "unknown")  # never a rate times nothing
    assert _verdict(result, "usage")["result"] == "warn"
    assert _verdict(result, "budget") == {
        "check": "budget",
        "result": "warn",
        "detail": "cap_musd=30;spent_musd=none",
    }
    said = cost_line(result, "claude")
    assert said == "[metabot] claude reported no usage, so there is no cost to show"


async def test_a_usage_report_with_no_counts_metabot_reads_is_not_called_none(tmp_path):
    from metabot.commands import cost_line

    result = await _quiet_run(tmp_path, {"total_tokens": 1234})
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    assert cost_line(result, "claude") == (
        "[metabot] claude's usage report has no token counts MetaBot can read, "
        "so there is no cost to show"
    )


# ── a run that never reached its model ──────────────────────────────────────


def _keyless(monkeypatch) -> OpenAIEngine:
    """A priced endpoint off this machine whose key is not set: no request is sent."""
    monkeypatch.delenv("METABOT_TEST_NO_KEY", raising=False)
    save_config(Config(prices=QUIET))
    return OpenAIEngine(
        base_url="https://models.example/v1", model="m-quiet", key_env="METABOT_TEST_NO_KEY",
        tool_mode="native", save_sessions=False, config=load_config(),
    )  # fmt: skip


async def test_a_run_that_never_reached_its_model_cost_nothing_under_a_cap(tmp_path, monkeypatch):
    from metabot.commands import cost_line

    result = await run_job(
        engine=_keyless(monkeypatch), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert "METABOT_TEST_NO_KEY is not set" in result.error
    assert (result.cost_musd, result.cost_source) == (0, "unsent")
    assert _verdict(result, "usage")["result"] == "pass"
    assert _verdict(result, "budget")["detail"] == "cap_musd=30;spent_musd=0"
    assert cost_line(result, "openai") == "[metabot] cost $0.00: openai's model was never reached"


async def test_an_engine_that_cannot_start_cost_nothing(tmp_path):
    class Missing(ClaudeEngine):
        binary = str(tmp_path / "no-such-claude")

    save_config(Config(prices=QUIET))
    result = await run_job(
        engine=Missing(), prompt="go", cwd=tmp_path, model="m-quiet", policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert not result.ok
    assert (result.cost_musd, result.cost_source) == (0, "unsent")


@pytest.mark.parametrize("status", [400, 401, 404, 429])
async def test_a_server_that_refused_every_request_ran_nothing(chat, tmp_path, status):
    reply = {"__status__": status, "error": {"message": "no"}}
    result = await run_job(
        engine=_priced(chat(repeat=reply), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert f"answered {status}" in result.error
    assert (result.cost_musd, result.cost_source) == (0, "unsent")


@pytest.mark.parametrize("status", [408, 500, 502, 504, 524])
async def test_a_server_error_after_the_request_went_out_keeps_an_unknown_cost(
    chat, tmp_path, status
):
    reply = {"__status__": status, "error": {"message": "upstream timed out"}}
    result = await run_job(
        engine=_priced(chat(repeat=reply), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert f"answered {status}" in result.error
    assert (result.cost_musd, result.cost_source) == (None, "unknown")  # the model may have run


async def test_a_proxy_that_refused_the_connection_ran_nothing(tmp_path, monkeypatch):
    import http.server
    import threading

    class Refuses(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):  # the name http.server calls for CONNECT
            self.send_response(403)
            self.end_headers()

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuses)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "https_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
        monkeypatch.setenv("METABOT_TEST_KEY", "k-test")
        save_config(Config(prices=QUIET))
        engine = OpenAIEngine(
            base_url="https://models.example/v1", model="m-quiet", key_env="METABOT_TEST_KEY",
            tool_mode="native", save_sessions=False, config=load_config(),
        )  # fmt: skip
        result = await run_job(
            engine=engine, prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
            limits=Limits(max_cost_musd=30),
        )  # fmt: skip
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert "cannot reach" in result.error
    assert (result.cost_musd, result.cost_source) == (0, "unsent")


async def test_a_server_that_cannot_be_reached_ran_nothing(tmp_path):
    save_config(Config(prices=TINY))
    engine = OpenAIEngine(base_url="http://127.0.0.1:9/v1", model="tiny", config=load_config())
    result = await run_job(
        engine=engine, prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
    )  # fmt: skip
    assert "cannot reach" in result.error
    assert (result.cost_musd, result.cost_source) == (0, "unsent")


async def test_a_run_whose_model_answered_once_keeps_an_unknown_cost(chat, tmp_path):
    script = [call("read_file", {"path": "x"}, "c1"), {"__status__": 502, "error": {}}]
    result = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(),
    )  # fmt: skip
    assert (result.cost_musd, result.cost_source) == (None, "unknown")  # its first reply ran


@pytest.mark.parametrize(
    "later",
    [
        {"__status__": 504, "error": {"message": "upstream timed out"}},
        {"__status__": 408, "error": {}},
        {"__raw__": "<html>a proxy's own page</html>"},
        {"choices": []},
        {"choices": [{"message": "an answer"}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": ["an", "answer"]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": "read_file"}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [{"function": "read_file"}]}}]},
    ],
    ids=[
        "504",
        "408",
        "not-json",
        "no-choices",
        "message-text",
        "message-null",
        "content-parts",
        "calls-text",
        "call-text",
    ],
)
async def test_a_later_turn_that_never_said_what_it_spent_leaves_the_cost_unknown(
    chat, tmp_path, later
):
    script = [_with_usage(call("read_file", {"path": "x"}, "c1"), 10, 5), later]
    result = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert not result.ok
    assert (result.cost_musd, result.cost_source) == (None, "unknown")  # the turn may have run
    assert _verdict(result, "budget") == {
        "check": "budget",
        "result": "warn",
        "detail": "cap_musd=30;spent_musd=none",
    }


@pytest.mark.parametrize(
    "said", [{}, {"usage": None}, {"usage": {"total_tokens": 40}}], ids=["none", "null", "total"]
)
async def test_a_later_turn_that_reads_but_never_says_what_it_spent_leaves_the_cost_unknown(
    chat, tmp_path, said
):
    from metabot.commands import cost_line

    later = {**call("read_file", {"path": "y"}, "c2"), **said}
    script = [_with_usage(call("read_file", {"path": "x"}, "c1"), 10, 5), later, answer("done")]
    capped = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert capped.error == "limit: $0.00003: a request that went out never said what it spent"
    assert (capped.cost_musd, capped.cost_source) == (None, "unknown")
    assert cost_line(capped, "openai") == (
        "[metabot] openai sent a request that never said what it spent, so there is no cost to show"
    )
    free = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(),
    )  # fmt: skip
    assert free.ok, free.error
    assert (free.cost_musd, free.cost_source) == (None, "unknown")


@pytest.mark.parametrize(
    "said", [{}, {"usage": None}, {"usage": {"total_tokens": 40}}], ids=["none", "null", "total"]
)
async def test_a_last_answer_that_never_says_what_it_spent_is_kept_under_a_cap(
    chat, tmp_path, said
):
    last = {"choices": [{"message": {"role": "assistant", "content": "done"}}], **said}
    script = [_with_usage(call("read_file", {"path": "x"}, "c1"), 10, 5), last]
    result = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), limits=Limits(max_cost_musd=1_000_000),
    )  # fmt: skip
    assert result.ok, result.error  # no call came after it for the cap to stop
    assert result.final_text == "done"
    assert (result.cost_musd, result.cost_source) == (None, "unknown")
    assert _verdict(result, "budget") == {
        "check": "budget",
        "result": "warn",
        "detail": "cap_musd=1000000;spent_musd=none",
    }


async def test_an_answer_with_no_message_still_counts_the_usage_it_reports(chat, tmp_path):
    later = {"choices": [], "usage": {"prompt_tokens": 100_000, "completion_tokens": 50_000}}
    script = [_with_usage(call("read_file", {"path": "x"}, "c1"), 10, 5), later]
    result = await run_job(
        engine=_priced(chat(script=script), TINY), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(),
    )  # fmt: skip
    assert not result.ok and result.tokens == 150_015
    assert (result.cost_musd, result.cost_source) == (200_020, "config")


# ── under a cap, a run needs a rate for its running count ───────────────────


@pytest.mark.parametrize("alias", ["haiku", "sonnet", "opus"])
async def test_a_model_claude_names_by_its_alias_starts_under_a_cap(tmp_path, monkeypatch, alias):
    total = {"usage": {"input_tokens": 0, "output_tokens": 100}, "total_cost_usd": 0.001}
    _claude_messages(tmp_path, monkeypatch, [100], total)
    result = await _claude_run(tmp_path, model=alias, limits=Limits(max_cost_musd=3_000))
    assert result.ok, result.error  # priced by the model it names as it starts
    assert (result.cost_musd, result.cost_source) == (1_000, "engine")


def test_an_alias_waits_for_the_model_claude_names_and_a_full_name_needs_its_rate():
    from metabot.prices import price_for
    from metabot.runner import unpriced

    for model in ("haiku", "sonnet", "opus", None):
        assert not unpriced(price_for("claude", model, None, {}), "claude", model)
    assert unpriced(price_for("claude", "claude-opus-9", None, {}), "claude", "claude-opus-9")
    assert unpriced(price_for("openai", "haiku", "https://x.example/v1", {}), "openai", "haiku")


async def test_a_named_model_with_no_rate_never_starts_under_a_cap(tmp_path, home):
    with pytest.raises(ValueError, match="no price for claude/claude-opus-9"):
        await _claude_run(tmp_path, model="claude-opus-9", limits=Limits(max_cost_musd=3_000))
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())


async def test_a_model_named_as_it_starts_with_no_rate_ends_at_its_first_call(
    tmp_path, monkeypatch
):
    total = {"usage": {"input_tokens": 0, "output_tokens": 300}, "total_cost_usd": 0.6}
    _claude_messages(tmp_path, monkeypatch, [100, 100, 100], total)
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(fixture.read_text().replace('"claude-opus-5-5"', '"claude-opus-9"'))
    result = await _claude_run(tmp_path, limits=Limits(max_cost_musd=3_000))
    assert result.error == "limit: $0.003: no price for claude/claude-opus-9 to count it by"
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [s["success"] for s in steps] == [False]  # its first call is not answered


async def test_a_model_the_engine_names_its_own_way_is_priced_as_the_one_asked_for(tmp_path):
    from metabot.events import Final, Started, Usage

    class Renames(_Reports):
        async def run(self, job, *, board=None, approve):
            yield Started("s-r", "Display Name 4.5")
            yield Usage({"input_tokens": 10, "output_tokens": 5})
            yield Final("done", "s-r")

    save_config(Config(prices=QUIET))
    result = await run_job(
        engine=Renames(), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
        model="m-quiet", limits=Limits(max_cost_musd=30),
    )  # fmt: skip
    assert (result.cost_musd, result.cost_source) == (20, "config")
