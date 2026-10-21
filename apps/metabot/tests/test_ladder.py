"""The ladder: the cheapest rung first, a climb only on a failure the record shows."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FAKE_CLI, FIXTURES, private_mode
from fake_chat import answer, text_reply
from metabot import commands
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, ConfigError, load_config, save_config
from metabot.ladder import (
    ANSWERED,
    CLIMBED,
    SKIPPED,
    STOPPED,
    LadderError,
    Rung,
    attempts,
    default_ladder,
    effective_ladder,
    judge,
    ladder_path,
    load_ladder,
    read_ladder,
    run_ladder,
    tier_of,
)
from metabot.models import save_probe
from metabot.paths import run_dir
from metabot.receipts import verify_file
from metabot.runner import NoPrice

LOCAL = "http://127.0.0.1:11434/v1"
CLAUDE_HERE = {"claude": "/bin/claude", "codex": None, "cursor": None}


# ── the rungs ───────────────────────────────────────────────────────────────


def test_a_ladder_the_person_set_reads_in_order_with_tiers_filled_in():
    cfg = Config(openai_base_url=LOCAL)
    rungs = read_ladder(
        [{"engine": "openai", "model": "qwen2.5:3b"}, {"engine": "claude", "model": "haiku"},
         {"engine": "claude", "tier": "paid"}],
        cfg,
    )  # fmt: skip
    assert rungs == [
        Rung("openai", "qwen2.5:3b", "local"),
        Rung("claude", "haiku", "sub"),
        Rung("claude", None, "paid"),
    ]


def test_the_openai_engine_off_this_machine_is_paid():
    assert tier_of("openai", Config(openai_base_url="https://models.example.invalid/v1")) == "paid"
    assert tier_of("openai", Config(openai_base_url=LOCAL)) == "local"
    assert tier_of("codex", Config()) == "sub"


@pytest.mark.parametrize(
    ("raw", "said"),
    [
        ({"engine": "claude"}, "not a list"),
        ([], "no rungs"),
        (["claude"], "rung 1 in config.json is not a table"),
        ([{"engine": "claude"}, {"engine": "gpt"}], "rung 2 in config.json names no engine"),
        ([{"engine": "claude", "model": "--yolo"}], "not a model name"),
        ([{"engine": "claude", "tier": "free"}], "tier that is not one of local, sub, paid"),
        ([{"engine": "claude", "budget": 3}], "a key MetaBot does not know: budget"),
    ],
)
def test_a_ladder_that_does_not_read_names_what_is_wrong(raw, said):
    with pytest.raises(LadderError, match=said):
        read_ladder(raw, Config())


def test_a_ladder_set_wrong_stops_the_command_that_runs_it(capsys):
    save_config(Config(ladder=[{"engine": "gpt"}]))
    assert main(["ladder"]) == 2
    assert "ladder rung 1 in config.json names no engine" in capsys.readouterr().err
    assert main(["ask", "--ladder", "hi"]) == 2


def test_attempts_are_three_unless_set_and_never_past_five(capsys):
    assert attempts(Config()) == 3
    assert main(["config", "set", "ladder_attempts", "2"]) == 0
    assert load_config().ladder_attempts == 2
    assert main(["config", "set", "ladder_attempts", "6"]) == 2
    with pytest.raises(LadderError):
        attempts(Config(ladder_attempts=0))
    (Path(load_config.__globals__["metabot_home"]()) / "config.json").write_text(
        json.dumps({"ladder_attempts": "many"})
    )
    with pytest.raises(ConfigError, match="ladder_attempts is not a whole number"):
        load_config()


def test_the_default_ladder_is_the_probed_local_model_then_the_build_role():
    cfg = Config(openai_base_url=LOCAL, openai_model="qwen2.5:3b")
    save_probe(LOCAL, "qwen2.5:3b", "native", "called read_file")
    rungs, note = default_ladder(cfg, CLAUDE_HERE, True)
    assert rungs == [Rung("openai", "qwen2.5:3b", "local"), Rung("claude", None, "sub")]
    assert note is None


def test_an_unprobed_local_model_is_left_off_and_the_ladder_says_how_to_probe_it():
    cfg = Config(openai_base_url=LOCAL, openai_model="qwen2.5:3b")
    rungs, note = default_ladder(cfg, CLAUDE_HERE, True)
    assert rungs == [Rung("claude", None, "sub")]
    assert note == "the local model qwen2.5:3b joins the ladder once probed: " + (
        "metabot doctor --models qwen2.5:3b"
    )


def test_a_local_model_whose_probe_found_no_tool_calls_is_left_off():
    cfg = Config(openai_base_url=LOCAL, openai_model="tiny")
    save_probe(LOCAL, "tiny", "none", "made no read")
    rungs, note = default_ladder(cfg, CLAUDE_HERE, True)
    assert rungs == [Rung("claude", None, "sub")]
    assert note is not None and "found no tool calls" in note


def test_a_local_model_server_that_is_not_answering_is_left_off():
    save_probe(LOCAL, "qwen2.5:3b", "native", "ok")
    rungs, _ = default_ladder(Config(openai_base_url=LOCAL), CLAUDE_HERE, False)
    assert rungs == [Rung("claude", None, "sub")]


def test_with_only_the_local_model_here_it_is_the_one_rung():
    save_probe(LOCAL, "qwen2.5:3b", "json", "by schema")
    nothing = {"claude": None, "codex": None, "cursor": None}
    rungs, _ = default_ladder(Config(openai_base_url=LOCAL), nothing, True)
    assert rungs == [Rung("openai", "qwen2.5:3b", "local")]


def test_the_ladder_command_shows_each_rung_its_probe_and_whose(capsys):
    save_config(Config(openai_base_url=LOCAL, ladder=[{"engine": "openai"}, {"engine": "claude"}]))
    save_probe(LOCAL, "qwen2.5:3b", "repaired", "as text")
    assert main(["ladder"]) == 0
    out = capsys.readouterr().out
    assert "rung 1  local  openai/qwen2.5:3b  probe repaired  (yours)" in out  # the set model
    assert "rung 2  sub    claude  (yours)" in out
    assert "at most 3 rung(s) run for one task" in out
    assert effective_ladder()[1] == "yours"


# ── what a rung's run means ─────────────────────────────────────────────────


def _receipt(tmp_path: Path, *verdicts: dict) -> Path:
    path = tmp_path / f"r{len(list(tmp_path.glob('r*.json')))}.json"
    path.write_text(json.dumps({"verdicts": list(verdicts)}))
    return path


def _ran(tmp_path, *verdicts, **kw) -> SimpleNamespace:
    base = {"ok": True, "declined": 0, "failed": 0, "limit": None, "error": None, "run_id": "L.1",
            "cost_musd": 0, "cost_source": "local"}  # fmt: skip
    return SimpleNamespace(**{**base, **kw, "receipt": _receipt(tmp_path, *verdicts)})


def test_a_run_that_ended_well_answers(tmp_path):
    assert judge(_ran(tmp_path), gated=True) == (ANSWERED, "")


def test_a_declined_call_beside_a_good_answer_leaves_the_answer(tmp_path):
    declined = {"check": "scope_approval:c1", "result": "warn", "detail": "declined"}
    assert judge(_ran(tmp_path, declined, declined=1), gated=True)[0] == ANSWERED


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"ok": False, "error": "openai: the model answered with a call MetaBot could not read"},
         "could not read"),
        ({"ok": False, "error": None}, "the run did not end well"),
        ({"failed": 2}, "2 call(s) did not succeed"),
    ],
)  # fmt: skip
def test_a_failure_on_a_gated_engine_climbs(tmp_path, kw, why):
    outcome, said = judge(_ran(tmp_path, **kw), gated=True)
    assert outcome == CLIMBED and why in said


def test_a_failed_declared_acceptance_climbs(tmp_path):
    failed = {"check": "declared_acceptance", "result": "fail", "detail": "x"}
    assert judge(_ran(tmp_path, failed), gated=True) == (CLIMBED, "its declared acceptance failed")


def test_a_declined_call_in_a_failed_run_stops_the_ladder(tmp_path):
    declined = {"check": "scope_approval:c1", "result": "warn", "detail": "declined"}
    outcome, why = judge(_ran(tmp_path, declined, ok=False, declined=1), gated=True)
    assert outcome == STOPPED and "1 call(s) declined" in why


@pytest.mark.parametrize(
    "verdict",
    [
        {"check": "scope_allow:c1", "result": "pass", "detail": "allow_write"},
        {"check": "scope_approval:c1", "result": "pass", "detail": "did:key:z"},
    ],
)
def test_a_failed_run_that_let_a_write_through_stops_the_ladder(tmp_path, verdict):
    outcome, why = judge(_ran(tmp_path, verdict, ok=False, error="boom"), gated=True)
    assert outcome == STOPPED
    assert why == "it failed after 1 write(s) went through; see L.1 first"


def test_a_failed_run_on_an_engine_whose_tools_run_unasked_stops_the_ladder(tmp_path):
    outcome, why = judge(_ran(tmp_path, ok=False, error="boom"), gated=False)
    assert outcome == STOPPED and "own tools" in why


def test_a_run_past_its_money_stops_the_ladder(tmp_path):
    outcome, why = judge(_ran(tmp_path, ok=False, limit="cost cap $0.01 reached"), gated=True)
    assert outcome == STOPPED and "cost cap" in why


# ── climbing ────────────────────────────────────────────────────────────────


def _fake_runs(tmp_path, outcomes: dict[int, dict], costs: dict[int, int | None] | None = None):
    """A run_rung whose n-th rung ends as ``outcomes[n]`` says, recording what it was given."""
    given: list[tuple[Rung, str, dict, int | None]] = []

    async def run_rung(rung, run_id, meta, cap):
        given.append((rung, run_id, dict(meta), cap))
        n = meta["rung"]
        if outcomes.get(n) == "noprice":
            raise NoPrice(f"no price for {rung.label()}")
        cost = (costs or {}).get(n, 0)
        return _ran(tmp_path, **{"run_id": run_id, "cost_musd": cost, "cost_source": "table",
                                 **outcomes.get(n, {})})  # fmt: skip

    return run_rung, given


def _ladder(rungs, run_rung, **kw):
    kw.setdefault("gated", lambda engine: True)
    return asyncio.run(
        run_ladder(rungs, run_rung, cfg=Config(openai_base_url=LOCAL), ladder_id="L", **kw)
    )


THREE = [Rung("openai", "tiny", "local"), Rung("claude", None, "sub"), Rung("codex", None, "sub")]


def test_a_rung_that_answers_ends_the_ladder(tmp_path):
    run_rung, given = _fake_runs(tmp_path, {})
    result = _ladder(THREE, run_rung)
    assert result.answered and len(given) == 1
    assert [r.outcome for r in result.rows] == [ANSWERED]


def test_a_failed_rung_climbs_and_the_next_is_linked_to_it(tmp_path):
    run_rung, given = _fake_runs(tmp_path, {1: {"ok": False, "error": "unreadable"}})
    lines: list[str] = []
    result = _ladder(THREE, run_rung, on_line=lines.append)
    assert result.answered and result.final.run_id == "L.2"
    assert [g[1] for g in given] == ["L.1", "L.2"]
    assert given[0][2] == {"ladder": "L", "rung": 1}
    assert given[1][2] == {"ladder": "L", "rung": 2, "parent": "L.1"}
    assert lines == ["rung 1 openai/tiny failed: unreadable", "rung 2 claude answered (L.2)"]


def test_the_ladder_runs_no_more_rungs_than_its_attempts(tmp_path):
    bad = {"ok": False, "error": "no"}
    run_rung, given = _fake_runs(tmp_path, {1: bad, 2: bad, 3: bad})
    result = _ladder(THREE, run_rung, max_attempts=2)
    assert len(given) == 2 and not result.answered
    assert result.stopped == "2 rung(s) tried, the most this ladder takes"


def test_a_ladder_that_runs_out_of_rungs_says_so(tmp_path):
    bad = {"ok": False, "error": "no"}
    run_rung, _ = _fake_runs(tmp_path, {1: bad, 2: bad, 3: bad})
    assert _ladder(THREE, run_rung).stopped == "no rung left to try"


def test_a_stop_never_starts_the_next_rung(tmp_path):
    declined = {"check": "scope_approval:c1", "result": "warn", "detail": "declined"}
    run_rung, given = _fake_runs(tmp_path, {})

    async def declines(rung, run_id, meta, cap):
        given.append((rung, run_id, meta, cap))
        return _ran(tmp_path, declined, ok=False, declined=1, run_id=run_id, cost_musd=0)

    result = _ladder(THREE, declines)
    assert len(given) == 1 and "declined" in (result.stopped or "")


def test_a_failed_codex_rung_stops_the_ladder(tmp_path):
    bad = {"ok": False, "error": "no"}
    run_rung, given = _fake_runs(tmp_path, {1: bad, 2: bad, 3: bad})
    rungs = [Rung("codex", None, "sub"), Rung("claude", None, "sub")]
    result = _ladder(rungs, run_rung, gated=lambda engine: engine != "codex")
    assert len(given) == 1 and "own tools" in (result.stopped or "")


def test_a_private_task_never_reaches_a_rung_off_this_machine(tmp_path):
    bad = {"ok": False, "error": "no"}
    run_rung, given = _fake_runs(tmp_path, {1: bad})
    result = _ladder(THREE, run_rung, private=True)
    assert [g[0].engine for g in given] == ["openai"]
    assert [r.outcome for r in result.rows] == [CLIMBED, SKIPPED, SKIPPED]
    assert "private" in result.rows[1].why


def test_the_money_cap_covers_the_whole_ladder(tmp_path):
    bad = {"ok": False, "error": "no"}
    run_rung, given = _fake_runs(tmp_path, {1: bad, 2: bad}, costs={1: 300_000, 2: 700_000})
    result = _ladder(THREE, run_rung, max_cost_musd=1_000_000)
    assert [g[3] for g in given] == [1_000_000, 700_000]
    assert result.spent_musd == 1_000_000
    assert result.stopped == "the ladder's $1.00 is spent"


def test_a_rung_that_cannot_be_priced_under_a_cap_is_skipped(tmp_path):
    run_rung, given = _fake_runs(tmp_path, {1: {"ok": False, "error": "no"}, 2: "noprice"})
    result = _ladder(THREE, run_rung, max_cost_musd=1_000_000, no_price=NoPrice)
    assert [r.outcome for r in result.rows] == [CLIMBED, SKIPPED, ANSWERED]
    assert result.rows[1].why == "no price for claude"
    assert given[2][2]["parent"] == "L.1"  # linked to the rung that ran


def test_the_ladder_record_is_private_and_lists_every_rung(tmp_path):
    run_rung, _ = _fake_runs(tmp_path, {1: {"ok": False, "error": "no"}}, costs={2: None})
    result = _ladder(THREE, run_rung)
    assert private_mode(ladder_path("L")) == 0o600
    record = load_ladder("L")
    assert record is not None and record["answered"] is True
    assert [r["outcome"] for r in record["rungs"]] == [CLIMBED, ANSWERED]
    assert record["unpriced"] == 1 and result.unpriced == 1


def test_a_ladder_id_in_use_is_refused(tmp_path):
    run_rung, _ = _fake_runs(tmp_path, {})
    _ladder(THREE, run_rung)
    with pytest.raises(LadderError, match="exists already"):
        _ladder(THREE, run_rung)


# ── ask --ladder, end to end ────────────────────────────────────────────────


@pytest.fixture
def claude_here(tmp_path, monkeypatch):
    """A PATH holding only a ``claude`` that is the fake CLI, answering ``metabot-probe``."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    return d


def _journal_meta(run_id: str) -> dict:
    return json.loads((run_dir(run_id) / "journal.json").read_text())["meta"]


def test_ask_ladder_climbs_from_a_local_model_that_garbles_its_call(
    chat, claude_here, tmp_path, capsys
):
    fake = chat([text_reply('{"name": "rm_rf", "arguments": {}}')])
    save_config(
        Config(
            openai_base_url=fake.url,
            ladder=[{"engine": "openai", "model": "tiny"}, {"engine": "claude"}],
        )
    )
    assert main(["ask", "--ladder", "--json", "--cwd", str(tmp_path), "say", "hi"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["final_text"] == "metabot-probe" and out["valid"] is True
    ladder = out["ladder"]
    assert [r["outcome"] for r in ladder["rungs"]] == [CLIMBED, ANSWERED]
    assert "could not read" in ladder["rungs"][0]["why"]
    first, second = (r["run_id"] for r in ladder["rungs"])
    assert second == f"{ladder['ladder_id']}.2"
    assert _journal_meta(second)["parent"] == first
    assert _journal_meta(first)["ladder"] == ladder["ladder_id"]
    for run_id in (first, second):  # each rung's receipt verifies alone
        receipt = Path(load_config.__globals__["metabot_home"]()) / "receipts" / f"{run_id}.json"
        assert verify_file(receipt).valid
    assert fake.requests[0]["model"] == "tiny"


def test_ask_ladder_says_each_rung_and_what_the_ladder_cost(chat, claude_here, tmp_path, capsys):
    fake = chat([answer("four")])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    assert main(["ask", "--ladder", "--cwd", str(tmp_path), "2+2"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "four\n"
    assert "[metabot] ladder (yours): local openai/tiny" in captured.err
    assert "[metabot] rung 1 openai/tiny answered" in captured.err
    assert "cost $0.00" in captured.err


def test_ask_ladder_with_no_answer_fails_and_says_why(chat, claude_here, tmp_path, capsys):
    fake = chat([text_reply('{"name": "rm_rf", "arguments": {}}')])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    assert main(["ask", "--ladder", "--cwd", str(tmp_path), "hi"]) == 1
    assert "[metabot] the ladder stopped: no rung left to try" in capsys.readouterr().err


def test_a_private_ask_ladder_keeps_off_the_rungs_that_leave(chat, claude_here, tmp_path, capsys):
    fake = chat([text_reply('{"name": "rm_rf", "arguments": {}}')])
    save_config(
        Config(
            openai_base_url=fake.url,
            ladder=[{"engine": "openai", "model": "tiny"}, {"engine": "claude"}],
        )
    )
    code = main(["ask", "--ladder", "--json", "--cwd", str(tmp_path), "summarise my secrets file"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert [r["outcome"] for r in out["ladder"]["rungs"]] == [CLIMBED, SKIPPED]


@pytest.mark.parametrize(
    "flags",
    [["-e", "claude"], ["-m", "haiku"], ["--suggest"], ["--resume", "s"], ["--role", "build"]],
)
def test_ask_ladder_picks_each_rung_itself(flags, capsys):
    assert main(["ask", "--ladder", *flags, "hi"]) == 2
    assert "--ladder picks each rung itself" in capsys.readouterr().err
