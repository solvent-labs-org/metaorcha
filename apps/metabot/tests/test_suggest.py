"""Suggestions: a role from a few rules, with the rule that chose it."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.paths import run_path
from metabot.receipts import list_index, verify_file
from metabot.suggest import RULES, Suggestion, suggest

FULL = {
    "fast": {"engine": "claude", "model": "haiku"},
    "build": {"engine": "claude"},
    "review": {"engine": "codex"},
    "second": {"engine": "cursor"},
    "private": {"engine": "openai", "model": "qwen2.5:3b"},
}


@pytest.mark.parametrize(
    ("task", "rule", "word"),
    [
        ("keep this private", "private", "private"),
        ("work offline on it", "private", "offline"),
        ("rotate the secrets file", "private", "secrets"),
        ("a confidential memo", "private", "confidential"),
        ("review my patch", "review", "review"),
        ("audit the login flow", "review", "audit"),
        ("critique this draft", "review", "critique"),
        ("check the tests pass", "review", "check"),
        ("triage these issues", "fast", "triage"),
        ("summarise the thread", "fast", "summarise"),
        ("summarize the thread", "fast", "summarize"),
        ("explain this error", "fast", "explain"),
        ("list the open branches", "fast", "list"),
        ("find the config loader", "fast", "find"),
        ("what is a closure", "fast", "what is"),
        ("implement the parser", "build", "implement"),
        ("build a cli", "build", "build"),
        ("refactor the runner", "build", "refactor"),
        ("fix the flaky test", "build", "fix"),
        ("write a test", "build", "write"),
        ("add a flag", "build", "add"),
    ],
)
def test_each_rule_fires_with_its_reason(task, rule, word):
    s = suggest(task, FULL)
    assert (s.rule, s.matched, s.role) == (rule, word, rule)
    assert (s.engine, s.model) == (FULL[rule]["engine"], FULL[rule].get("model"))
    assert s.note is None


def test_words_match_whole_words_in_any_case():
    assert suggest("checkout the branch", FULL).rule == "default"  # not "check"
    assert suggest("listen to the address", FULL).rule == "default"  # not "list", not "add"
    assert suggest("Reviewing it", FULL).matched == "Reviewing"
    assert suggest("Summarizing the notes", FULL).rule == "fast"
    assert suggest("", FULL) == Suggestion("build", "claude", None, "default", None)


def test_first_matching_rule_wins():
    assert [rule.name for rule in RULES] == ["private", "review", "fast", "build"]
    s = suggest("review the confidential contract and fix typos", FULL)
    assert (s.rule, s.role, s.matched) == ("private", "private", "confidential")
    s = suggest("fix it, then check the build", FULL)
    assert (s.rule, s.matched) == ("review", "check")  # rule order, not word order


def test_role_missing_from_roster_falls_back_to_build_and_says_so():
    s = suggest("audit the login flow", {"build": {"engine": "claude", "model": "m"}})
    assert (s.role, s.engine, s.model, s.rule, s.matched) == (
        "build",
        "claude",
        "m",
        "review",
        "audit",
    )
    assert s.note == "the review role has no engine here, so the build role instead"
    s = suggest("audit it", {})
    assert s.engine is None and s.role == "review"
    assert s.note.startswith("no engine for the review role here, nor for the build role")
    s = suggest("tidy up", {})
    assert s.engine is None and s.note.startswith("no engine for the build role here")


def test_a_private_task_never_falls_back_off_this_machine():
    s = suggest("summarise this confidential memo", {"build": {"engine": "claude"}})
    assert (s.role, s.engine, s.rule, s.matched) == ("private", None, "private", "confidential")
    assert "no engine here keeps the work on this machine" in s.note


def test_a_private_task_on_an_engine_that_leaves_says_so(capsys):
    def local(entry):
        return entry["engine"] == "openai"

    roster = {"private": {"engine": "claude"}, "build": {"engine": "claude"}}
    s = suggest("a confidential memo", roster, local=local)
    assert (s.role, s.engine) == ("private", "claude")
    assert s.note == "the private role is set to an engine that sends the work off this machine"
    assert suggest("a confidential memo", FULL, local=local).note is None
    assert suggest("fix it", roster, local=local).note is None  # only the private role says so
    save_config(Config(roster=roster))
    assert main(["suggest", "a", "confidential", "memo"]) == 0
    assert "note    the private role is set to an engine that sends" in capsys.readouterr().out


def test_suggest_prints_role_engine_model_and_rule(capsys):
    save_config(Config(roster=FULL))
    assert main(["suggest", "audit", "the", "login", "flow"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "role    review",
        "engine  codex",
        "model   (its own)",
        'rule    review: the task says "audit"',
    ]
    assert main(["suggest", "tidy", "up"]) == 0
    assert "rule    default: no rule matched, so the build role" in capsys.readouterr().out
    save_config(Config())
    assert main(["suggest", "audit", "it"]) == 1  # nothing here can run it
    out = capsys.readouterr().out
    assert "engine  -" in out and "note    no engine for the review role here" in out


@pytest.fixture
def ask(tmp_path, monkeypatch):
    """``metabot ask`` with stand-in engine CLIs; returns the exit code and the run's index row."""
    engines = {"claude": ClaudeEngine, "codex": CodexEngine}
    fixtures = {"claude": "claude-echo.jsonl", "codex": "codex-echo.jsonl"}

    def get(name):
        monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / fixtures[name]))
        return wrap(engines[name])

    monkeypatch.setattr("metabot.commands.get_engine", get)

    def run(*argv: str, quiet: bool = True) -> tuple[int, dict | None]:
        before = len(list_index())
        code = main(["ask", *(["-q"] if quiet else []), "--cwd", str(tmp_path), *argv])
        rows = list_index()
        return code, rows[-1] if len(rows) > before else None

    return run


def _journal(row: dict) -> dict:
    return json.loads((run_path(row["run_id"]) / "journal.json").read_text(encoding="utf-8"))


def _model_verdicts(row: dict) -> list[dict]:
    receipt = json.loads(Path(row["receipt"]).read_text(encoding="utf-8"))
    return [v for v in receipt["verdicts"] if v["check"] == "model"]


def test_journal_records_suggested_and_chosen(ask):
    save_config(Config(roster=FULL))
    code, row = ask("--suggest", "audit", "the", "parser")
    assert code == 0
    meta = _journal(row)["meta"]
    assert meta["suggested"] == {
        "role": "review",
        "engine": "codex",
        "model": None,
        "rule": "review",
        "matched": "audit",
    }
    assert meta["chosen"] == {"engine": "codex", "model": None}
    assert (row["engine"], row["role"]) == ("codex", "review")
    code, row = ask("--suggest", "-e", "claude", "-m", "sonnet", "audit", "it")
    assert code == 0
    meta = _journal(row)["meta"]
    assert meta["suggested"]["engine"] == "codex"
    assert meta["chosen"] == {"engine": "claude", "model": "sonnet"}
    [signed] = _model_verdicts(row)  # only the chosen model is signed
    assert signed["detail"].startswith("claude/")
    receipt = Path(row["receipt"]).read_text(encoding="utf-8")
    assert "suggest" not in receipt and verify_file(row["receipt"]).valid


def test_a_run_without_suggest_records_neither(ask):
    save_config(Config(roster=FULL))
    code, row = ask("--role", "review", "audit", "it")
    assert code == 0
    meta = _journal(row)["meta"]
    assert "suggested" not in meta and "chosen" not in meta


def test_ask_suggest_says_what_it_chose(ask, capsys):
    save_config(Config(roster={"build": {"engine": "claude"}}))
    code, _ = ask("--suggest", "audit", "it", quiet=False)
    assert code == 0
    err = capsys.readouterr().err
    assert '[metabot] suggested the build role (rule review: the task says "audit"): claude' in err
    assert "[metabot] the review role has no engine here, so the build role instead" in err


def test_ask_suggest_refuses_when_nothing_can_run_it(ask, capsys):
    code, row = ask("--suggest", "audit", "it")
    assert code == 2 and row is None
    assert "no engine for the review role here" in capsys.readouterr().err
    save_config(Config(roster={"build": {"engine": "claude"}}))
    code, row = ask("--suggest", "summarise", "this", "confidential", "memo")
    assert code == 2 and row is None  # a private task never leaves on a suggestion
    assert "keeps the work on this machine" in capsys.readouterr().err
    code, row = ask("--suggest", "--role", "fast", "hi")
    assert code == 2 and row is None
    assert "--suggest picks the role itself" in capsys.readouterr().err


def test_ask_suggest_reads_stdin_once(ask, monkeypatch):
    save_config(Config(roster=FULL))
    monkeypatch.setattr(sys, "stdin", io.StringIO("explain closures\n"))
    code, row = ask("--suggest", "-")
    assert code == 0
    assert (row["role"], row["prompt"]) == ("fast", "explain closures\n")
