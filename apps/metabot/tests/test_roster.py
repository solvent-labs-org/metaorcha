"""The roster: roles map to an engine and a model; the defaults use only what is here."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
import yaml

from conftest import FIXTURES
from fake_engines import wrap
from metabot import roster as roster_mod
from metabot.cli import main
from metabot.config import Config, load_config, save_config
from metabot.console import Trace
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.paths import metabot_home
from metabot.receipts import list_index
from metabot.repl import repl
from metabot.roster import ROLES, default_roster, effective_roster, pick

NONE = {"claude": None, "codex": None, "cursor": None}
ALL = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": "/bin/cursor-agent"}
ONLY_CLAUDE = {**NONE, "claude": "/bin/claude"}


def _here(monkeypatch, installed, reachable: bool = False) -> None:
    monkeypatch.setattr(roster_mod, "here", lambda cfg: (dict(installed), reachable))


def test_the_roles():
    assert ROLES == ("fast", "build", "review", "second", "private")


def test_default_uses_only_installed_engines(monkeypatch, capsys):
    assert default_roster(NONE, False, "qwen2.5:3b") == {}
    assert default_roster(ONLY_CLAUDE, False) == {
        "fast": {"engine": "claude", "model": "haiku"},
        "build": {"engine": "claude"},
        "review": {"engine": "claude"},
    }
    assert default_roster(ALL, False) == {
        "fast": {"engine": "claude", "model": "haiku"},
        "build": {"engine": "claude"},
        "review": {"engine": "codex"},
        "second": {"engine": "cursor"},
    }
    only_codex = {**NONE, "codex": "/bin/codex"}
    assert default_roster(only_codex, False) == {
        "build": {"engine": "codex"},
        "review": {"engine": "codex"},
    }
    _here(monkeypatch, NONE)
    assert main(["roster"]) == 0
    out = capsys.readouterr().out
    assert "the roster is empty: no engine CLI is installed" in out
    assert "no local model server answered at http://127.0.0.1:11434/v1" in out
    assert all(line.split()[1] == "-" for line in out.splitlines()[:5])


def test_private_is_the_local_model_only_when_reachable():
    assert "private" not in default_roster(ALL, False, "qwen2.5:3b")
    assert default_roster(NONE, True, "qwen2.5:3b") == {
        "build": {"engine": "openai", "model": "qwen2.5:3b"},  # nothing else here builds
        "private": {"engine": "openai", "model": "qwen2.5:3b"},
    }
    both = default_roster(ONLY_CLAUDE, True, "m")
    assert both["build"] == {"engine": "claude"} and both["private"] == {
        "engine": "openai",
        "model": "m",
    }


def test_person_edits_win_and_are_marked(monkeypatch, capsys):
    _here(monkeypatch, ONLY_CLAUDE)
    assert main(["roster", "set", "fast", "--engine", "codex", "--model", "m-fast"]) == 0
    assert main(["roster", "set", "second", "--engine", "cursor"]) == 0
    capsys.readouterr()
    roster = effective_roster(load_config(), ONLY_CLAUDE, False)
    assert roster["fast"] == ({"engine": "codex", "model": "m-fast"}, "yours")
    assert roster["build"] == ({"engine": "claude"}, "default")
    assert main(["roster"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert rows["fast"].split()[1:4] == ["codex", "m-fast", "yours"]
    assert rows["fast"].endswith("codex is not installed")
    assert rows["build"].split()[1:4] == ["claude", "(its", "own)"] and "default" in rows["build"]
    assert (
        rows["second"].split()[1] == "cursor" and "cursor-agent is not installed" in rows["second"]
    )
    assert rows["private"].split()[1] == "-"
    assert main(["roster", "unset", "fast"]) == 0
    assert effective_roster(load_config(), ONLY_CLAUDE, False)["fast"][1] == "default"
    assert main(["roster", "unset", "fast"]) == 2  # nothing of yours left to unset


def test_a_private_role_off_this_machine_says_so(monkeypatch, capsys):
    _here(monkeypatch, ONLY_CLAUDE, reachable=True)
    assert main(["roster", "set", "private", "--engine", "claude"]) == 0
    assert "note: this engine sends the work off this machine" in capsys.readouterr().out
    assert main(["roster"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert rows["private"].endswith("this engine sends the work off this machine")
    save_config(Config(openai_base_url="https://models.example.invalid/v1"))
    assert main(["roster", "set", "private", "--engine", "openai", "--model", "m"]) == 0
    assert "note: this engine sends the work off this machine" in capsys.readouterr().out
    save_config(Config(roster={"private": {"engine": "openai", "model": "m"}}))
    assert main(["roster"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert rows["private"].endswith("work that never leaves this machine")


def test_unknown_engine_in_set_is_refused(capsys):
    assert main(["roster", "set", "fast", "--engine", "nope"]) == 2
    assert "unknown engine 'nope'" in capsys.readouterr().err
    assert main(["roster", "set", "boss", "--engine", "claude"]) == 2
    assert "no role 'boss'" in capsys.readouterr().err
    assert main(["roster", "set", "fast", "--engine", "claude", "--model=--yolo"]) == 2
    assert "is not a model name" in capsys.readouterr().err
    assert load_config().roster == {}


def test_a_hand_edited_entry_with_a_bad_model_keeps_the_engine_only(monkeypatch):
    save_config(Config(roster={"fast": {"engine": "claude", "model": "-x y"}, "build": {}}))
    roster = effective_roster(load_config(), NONE, False)
    assert roster == {"fast": ({"engine": "claude"}, "yours")}


def test_a_roster_that_is_not_a_table_counts_as_none(monkeypatch, capsys):
    (metabot_home() / "config.json").write_text('{"roster": ["fast"]}', encoding="utf-8")
    _here(monkeypatch, ONLY_CLAUDE)
    assert main(["roster"]) == 0
    assert "yours" not in capsys.readouterr().out
    assert main(["roster", "unset", "fast"]) == 2
    assert main(["roster", "set", "fast", "--engine", "codex"]) == 0
    assert load_config().roster == {"fast": {"engine": "codex"}}


@pytest.fixture
def argv_of(tmp_path, monkeypatch):
    """``metabot ask`` with stand-in engine CLIs; returns the argv the engine got."""
    out = tmp_path / "argv.json"
    engines = {"claude": ClaudeEngine, "codex": CodexEngine}
    fixtures = {"claude": "claude-echo.jsonl", "codex": "codex-echo.jsonl"}

    def get(name):
        monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / fixtures[name]))
        return wrap(engines[name])

    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    monkeypatch.setattr("metabot.commands.get_engine", get)

    def run(*flags: str) -> tuple[int, list[str]]:
        code = main(["ask", "-q", "--cwd", str(tmp_path), *flags, "hi"])
        argv = json.loads(out.read_text())["argv"] if out.exists() else []
        out.unlink(missing_ok=True)
        return code, argv

    return run


def test_ask_role_picks_engine_and_model(argv_of):
    save_config(Config(roster={"fast": {"engine": "claude", "model": "haiku"}}))
    code, argv = argv_of("--role", "fast")
    assert code == 0
    assert argv[argv.index("--model") + 1] == "haiku"
    [row] = list_index()
    assert (row["engine"], row["model_requested"], row["role"]) == ("claude", "haiku", "fast")


def test_ask_role_uses_the_default_roster(argv_of, monkeypatch):
    _here(monkeypatch, ALL)
    code, argv = argv_of("--role", "review")
    assert code == 0
    [row] = list_index()
    assert (row["engine"], row["role"]) == ("codex", "review")


def test_explicit_e_and_m_beat_the_role(argv_of):
    save_config(Config(roster={"fast": {"engine": "claude", "model": "haiku"}}))
    code, argv = argv_of("--role", "fast", "-m", "sonnet")
    assert code == 0 and argv[argv.index("--model") + 1] == "sonnet"
    code, argv = argv_of("--role", "fast", "-e", "codex")
    assert code == 0 and "haiku" not in argv  # the role's model goes with its engine only
    assert [r["engine"] for r in list_index()] == ["claude", "codex"]
    assert pick("fast", "codex", None) == ("codex", None)
    assert pick(None, None, "m") == (None, "m")


def test_a_role_with_nothing_here_is_refused(argv_of, capsys):
    code, argv = argv_of("--role", "second")
    assert code == 2 and argv == []
    assert "no engine for the second role here" in capsys.readouterr().err
    code, _ = argv_of("--role", "boss")
    assert code == 2 and "no role 'boss'" in capsys.readouterr().err


def test_an_any_desk_runs_on_the_default_build_role(tmp_path, monkeypatch, capsys):
    root = tmp_path / "office"
    root.mkdir()
    (root / "roles").mkdir()
    (root / "roles" / "x.md").write_text("You are x.\n")
    data = {
        "name": "t",
        "gates": {"ready": {"owner": ["verdict"], "engineer": ["tests"]}},
        "desks": {"pool": {"role": "roles/x.md", "harness": "any"}},
    }
    (root / "office.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    assert main(["office", "check", "--dir", str(root)]) == 0
    assert "! pool:" in capsys.readouterr().out  # nothing here: the any desk cannot run
    _here(monkeypatch, ONLY_CLAUDE)
    assert main(["office", "check", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    pool = next(line for line in out.splitlines() if line.strip().startswith("pool "))
    assert "claude" in pool and "! pool:" not in out


def test_chat_role_picks_engine_and_model(monkeypatch, capsys):
    save_config(Config(roster={"fast": {"engine": "codex", "model": "m-fast"}}))
    seen: dict = {}
    monkeypatch.setattr("metabot.repl.repl", lambda read, write, **kw: seen.update(kw) or 0)
    assert main(["chat", "--role", "fast"]) == 0
    assert seen["engine"] == "codex"
    assert (seen["options"]["model"], seen["options"]["role"]) == ("m-fast", "fast")
    assert main(["chat", "--role", "fast", "-e", "claude"]) == 0
    assert seen["engine"] == "claude" and seen["options"]["model"] is None
    assert main(["chat", "--role", "second"]) == 2
    assert "no engine for the second role here" in capsys.readouterr().out


def test_switching_engine_in_chat_drops_the_role_and_its_model(tmp_path):
    calls: list[dict] = []
    lines = ["one", "/engine codex", "two"]

    def read(_prompt: str) -> str:
        if not lines:
            raise EOFError
        return lines.pop(0)

    def run(**kw):
        calls.append(kw)
        return SimpleNamespace(
            final_text="",
            session_id=None,
            receipt=tmp_path / "none.json",
            steps=0,
            declined=0,
            failed=0,
        )

    options = {"model": "haiku", "role": "fast"}
    trace = Trace(out=io.StringIO())
    code = repl(
        read, lambda s: None, engine="claude", cwd=tmp_path, trace=trace, run=run, options=options
    )
    assert code == 0
    assert (calls[0]["engine_name"], calls[0]["model"], calls[0]["role"]) == (
        "claude",
        "haiku",
        "fast",
    )
    assert calls[1]["engine_name"] == "codex" and "role" not in calls[1] and "model" not in calls[1]
