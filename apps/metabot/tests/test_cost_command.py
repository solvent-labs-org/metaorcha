"""``ask --max-cost`` and ``metabot cost``: what runs spent, read from the run index."""

from __future__ import annotations

import json
import sys

import pytest

from conftest import FAKE_CLI, FIXTURES
from metabot import commands
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config


@pytest.fixture
def claude_here(tmp_path, monkeypatch):
    """A PATH whose ``claude`` and ``codex`` are the fake CLI."""
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("claude", "codex"):
        shim = d / name
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    save_config(Config(openai_base_url="http://127.0.0.1:9/v1"))
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    return d


def _ask(tmp_path, capsys, *extra: str) -> tuple[int, dict | None, str]:
    code = main(["ask", "--json", "--cwd", str(tmp_path), *extra, "hi"])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_ask_reports_what_the_run_cost(claude_here, tmp_path, capsys):
    code, result, _ = _ask(tmp_path, capsys)
    assert code == 0
    assert (result["cost_musd"], result["cost_source"]) == (1695, "engine")


def test_ask_says_the_cost_beside_the_receipt(claude_here, tmp_path, capsys):
    assert main(["ask", "--cwd", str(tmp_path), "hi"]) == 0
    err = capsys.readouterr().err
    assert "[metabot] cost $0.001695, as claude reported it" in err


def test_ask_under_its_money_cap_runs(claude_here, tmp_path, capsys):
    code, result, _ = _ask(tmp_path, capsys, "--max-cost", "0.50")
    assert code == 0 and result["limit"] is None


def test_ask_past_its_money_cap_fails_with_the_cap(claude_here, tmp_path, capsys):
    code, result, _ = _ask(tmp_path, capsys, "--max-cost", "0.001")
    assert code == 1
    assert result["error"] == "limit: $0.001"


def test_a_money_cap_with_no_price_never_starts(claude_here, tmp_path, capsys, home):
    code = main(
        ["ask", "-e", "codex", "-m", "gpt-5", "--max-cost", "1", "--cwd", str(tmp_path), "hi"]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "no price for codex/gpt-5" in err
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "nan", "inf"])
def test_a_money_cap_that_is_not_an_amount_is_refused(claude_here, tmp_path, capsys, bad):
    with pytest.raises(SystemExit) as stop:
        main(["ask", "--max-cost", bad, "--cwd", str(tmp_path), "hi"])
    assert stop.value.code == 2


def _row(home, **row) -> None:
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"run_id": f"mb-{row.get('n', 0)}", **row}) + "\n")


def test_cost_totals_by_engine_and_model_and_says_where_each_figure_comes_from(home, capsys):
    now = "2026-10-21T10:00:00Z"
    _row(home, n=1, engine="claude", model="claude-opus-5-5", tokens=100, cost_musd=2_000_000,
         cost_source="engine", finished_at=now)  # fmt: skip
    _row(home, n=2, engine="claude", model="claude-opus-5-5", tokens=50, cost_musd=500_000,
         cost_source="engine", finished_at=now)  # fmt: skip
    _row(home, n=3, engine="openai", model="qwen2.5:3b", tokens=900, cost_musd=0,
         cost_source="local", finished_at=now)  # fmt: skip
    _row(home, n=4, engine="codex", model="gpt-5", tokens=300, cost_musd=None,
         cost_source="plan", finished_at=now)  # fmt: skip
    _row(home, n=5, engine="claude", model="claude-haiku-5-5", tokens=7, finished_at=now)
    assert main(["cost"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any("claude/claude-opus-5-5" in x and "2 runs" in x and "$2.50" in x for x in lines)
    assert any("openai/qwen2.5:3b" in x and "$0.00" in x and "on this machine" in x for x in lines)
    assert any("codex/gpt-5" in x and "300 tokens" in x and "on your plan" in x for x in lines)
    assert any("claude/claude-haiku-5-5" in x and "not recorded" in x for x in lines)
    assert "total $2.50 over 5 runs" in out
    assert "prices as of 2026-10-21" in out


def test_cost_as_json(home, capsys):
    _row(home, n=1, engine="openai", model="m", tokens=10, cost_musd=0, cost_source="local",
         finished_at="2026-10-21T10:00:00Z")  # fmt: skip
    assert main(["cost", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_musd"] == 0 and data["runs"] == 1
    assert data["by_model"] == [
        {"engine": "openai", "model": "m", "runs": 1, "tokens": 10, "cost_musd": 0,
         "sources": ["local"]}
    ]  # fmt: skip


def test_cost_with_no_runs(home, capsys):
    assert main(["cost"]) == 0
    assert "no runs yet" in capsys.readouterr().out


def test_cost_shows_an_edited_index_safely(home, capsys):
    _row(home, n=1, engine="openai", model="m\x1b[31mred", tokens="lots", cost_musd="free",
         cost_source="local", finished_at="2026-10-21T10:00:00Z")  # fmt: skip
    assert main(["cost"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "total $0.00 over 1 runs" in out


def test_a_run_that_reported_no_usage_says_so_and_asks_for_no_price():
    from types import SimpleNamespace

    from metabot.commands import cost_line

    nothing = SimpleNamespace(cost_musd=None, cost_source="unknown", tokens=0)
    said = cost_line(nothing, "demo")
    assert said == "[metabot] demo reported no usage, so there is no cost to show"
    some = SimpleNamespace(cost_musd=None, cost_source="unknown", tokens=12)
    assert cost_line(some, "demo").endswith(
        "12 tokens; no price for this model: add one under prices"
    )
