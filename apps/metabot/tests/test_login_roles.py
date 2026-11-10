"""Logins on roles, on resumed sessions and in chat (S4b), and a login that picks its engine.

A login names its engine, so ``ask --login work`` runs on it without ``-e``. A role can
carry a login (``roster set build --engine claude --login work``), and ``ask --role build``
then runs on that login's folder; ``--login`` wins. A session that ran on a login lives in
that login's folder, so resuming it goes back there: the resume takes the session's login,
and a different one is refused before anything starts. ``chat --login`` keeps every turn on
the login.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import FAKE_CLI, FIXTURES
from metabot import commands
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import load_config, save_config
from metabot.receipts import list_index
from metabot.roster import role_login

SESSION = "00000000-0000-4000-8000-000000000001"  # the session claude-echo.jsonl reports


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "logins" / name
    f.mkdir(parents=True)
    os.chmod(f, 0o700)
    return f


def _add(name: str, engine: str, home: Path, *more: str) -> int:
    return main(["login", "add", name, "--engine", engine, "--home", str(home), *more])


@pytest.fixture
def work(tmp_path: Path, capsys) -> Path:
    f = _folder(tmp_path, "claude-work")
    assert _add("qxwork", "claude", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def play(tmp_path: Path, capsys) -> Path:
    f = _folder(tmp_path, "claude-play")
    assert _add("qxplay", "claude", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def side(tmp_path: Path, capsys) -> Path:
    f = _folder(tmp_path, "codex-side")
    assert _add("qxside", "codex", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A PATH holding only a ``claude`` that is the fake CLI, recording what it was given."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(seen))
    return seen


def _started(seen: Path) -> dict:
    started = json.loads(seen.read_text())
    seen.unlink()
    return started


def _ask(tmp_path: Path, *flags: str) -> int:
    return main(["ask", "--cwd", str(tmp_path), *flags, "hi"])


# ── a login picks its engine ────────────────────────────────────────────────


def test_ask_on_a_login_needs_no_engine_named(work, fake_claude, tmp_path, capsys):
    cfg = load_config()
    cfg.default_engine = "openai"  # a plain ask would go to the local model
    save_config(cfg)
    assert _ask(tmp_path, "--login", "qxwork") == 0
    assert _started(fake_claude)["claude_config_dir"] == str(work)


def test_naming_the_login_s_own_engine_too_is_fine(work, fake_claude, tmp_path):
    assert _ask(tmp_path, "-e", "claude", "--login", "qxwork") == 0
    assert _started(fake_claude)["claude_config_dir"] == str(work)


def test_naming_another_engine_with_a_login_never_starts(work, fake_claude, tmp_path, capsys):
    assert _ask(tmp_path, "-e", "codex", "--login", "qxwork") == 2
    assert "qxwork is a claude login, not one for codex" in capsys.readouterr().err
    assert not fake_claude.exists()


# ── a role carries a login ──────────────────────────────────────────────────


def test_roster_set_writes_the_login_with_the_role(work, capsys):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    assert load_config().roster["build"] == {"engine": "claude", "login": "qxwork"}
    assert "build: claude on the login qxwork (yours)" in capsys.readouterr().out


def test_roster_shows_a_role_s_login(work, capsys):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    capsys.readouterr()
    assert main(["roster"]) == 0
    [line] = [x for x in capsys.readouterr().out.splitlines() if x.startswith("build")]
    assert "login qxwork" in line


def test_roster_set_refuses_an_unknown_login(capsys):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxnone"]) == 2
    assert "there is no login 'qxnone'" in capsys.readouterr().err
    assert "build" not in (load_config().roster or {})


def test_roster_set_refuses_a_login_of_another_engine(work, capsys):
    assert main(["roster", "set", "build", "--engine", "codex", "--login", "qxwork"]) == 2
    assert "qxwork is a claude login, not one for codex" in capsys.readouterr().err


def test_ask_on_a_role_runs_on_its_login(work, fake_claude, tmp_path, capsys):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    assert _ask(tmp_path, "--role", "build") == 0
    assert _started(fake_claude)["claude_config_dir"] == str(work)
    assert "[metabot] login qxwork, from the build role" in capsys.readouterr().err


def test_login_on_the_command_line_wins_over_the_role_s(work, play, fake_claude, tmp_path):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    assert _ask(tmp_path, "--role", "build", "--login", "qxplay") == 0
    assert _started(fake_claude)["claude_config_dir"] == str(play)


def test_a_role_s_login_goes_with_its_own_engine_only(work):
    cfg = load_config()
    cfg.roster = {"build": {"engine": "claude", "login": "qxwork"}}
    save_config(cfg)
    assert role_login("build", "claude") == "qxwork"
    assert role_login("build", "codex") is None  # -e codex on the build role: its own sign-in
    assert role_login("fast", "claude") is None


def test_a_login_a_role_runs_on_cannot_be_removed(work, capsys):
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    capsys.readouterr()
    assert main(["login", "remove", "qxwork"]) == 2
    err = capsys.readouterr().err
    assert "the build role runs on it" in err
    assert "metabot roster set build --engine claude" in err
    assert "qxwork" in load_config().logins


# ── a resumed session goes back to its login ────────────────────────────────


def test_a_resume_goes_back_to_the_session_s_login(work, fake_claude, tmp_path, capsys):
    assert _ask(tmp_path, "--login", "qxwork") == 0
    _started(fake_claude)
    capsys.readouterr()
    assert _ask(tmp_path, "-e", "claude", "--resume", SESSION) == 0
    started = _started(fake_claude)
    assert started["claude_config_dir"] == str(work)
    assert SESSION in started["argv"]
    assert f"[metabot] login qxwork: session {SESSION} ran there" in capsys.readouterr().err


def test_a_resume_on_another_login_never_starts(work, play, fake_claude, tmp_path, capsys):
    assert _ask(tmp_path, "--login", "qxwork") == 0
    _started(fake_claude)
    capsys.readouterr()
    assert _ask(tmp_path, "--resume", SESSION, "--login", "qxplay") == 2
    err = capsys.readouterr().err
    assert f"session {SESSION} ran on the login qxwork" in err
    assert not fake_claude.exists()


def test_a_session_with_no_login_resumes_without_one(work, fake_claude, tmp_path, capsys):
    assert _ask(tmp_path, "-e", "claude") == 0
    _started(fake_claude)
    capsys.readouterr()
    assert _ask(tmp_path, "--resume", SESSION, "--login", "qxwork") == 2
    assert f"session {SESSION} ran with no login" in capsys.readouterr().err
    assert not fake_claude.exists()


def test_a_session_with_no_login_resumes_there_even_on_a_role_with_one(
    work, fake_claude, tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "own"))
    assert _ask(tmp_path, "-e", "claude") == 0
    _started(fake_claude)
    assert main(["roster", "set", "build", "--engine", "claude", "--login", "qxwork"]) == 0
    assert _ask(tmp_path, "--role", "build", "--resume", SESSION) == 0
    assert _started(fake_claude)["claude_config_dir"] == str(tmp_path / "own")


def test_a_session_metabot_never_ran_resumes_as_asked(work, fake_claude, tmp_path):
    assert _ask(tmp_path, "--login", "qxwork", "--resume", "some-other-session") == 0
    assert _started(fake_claude)["claude_config_dir"] == str(work)


# ── chat on a login ─────────────────────────────────────────────────────────


def _chat(monkeypatch, *argv: str, lines: tuple[str, ...] = ("hi", "again")) -> int:
    feed = iter(lines)

    def read(_prompt: str = "") -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return main(["chat", *argv])


def test_chat_on_a_login_keeps_every_turn_there(work, fake_claude, tmp_path, monkeypatch, capsys):
    assert _chat(monkeypatch, "--cwd", str(tmp_path), "--login", "qxwork") == 0
    rows = list_index()
    assert [(r.get("login"), r.get("login_folder")) for r in rows] == [("qxwork", str(work))] * 2
    assert "on the login qxwork" in capsys.readouterr().out


def test_chat_on_a_refusing_login_with_a_key_set_never_starts(
    work, fake_claude, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    assert _chat(monkeypatch, "--cwd", str(tmp_path), "--login", "qxwork") == 2
    assert "ANTHROPIC_API_KEY is set here" in capsys.readouterr().out
    assert not fake_claude.exists()


def test_chat_on_a_login_keeps_its_engine(work, fake_claude, tmp_path, monkeypatch, capsys):
    lines = ("/engine codex", "hi")
    assert _chat(monkeypatch, "--cwd", str(tmp_path), "--login", "qxwork", lines=lines) == 0
    out = capsys.readouterr().out
    assert "this chat runs on the claude login qxwork" in out
    assert json.loads(fake_claude.read_text())["claude_config_dir"] == str(work)


# ── the choice, said plainly at login add ───────────────────────────────────


def test_login_add_says_the_choice_with_the_login_s_own_name(tmp_path, capsys):
    assert _add("qxwork", "claude", _folder(tmp_path, "w")) == 0
    out = capsys.readouterr().out
    assert (
        "if another sign-in is set when a run starts: refuse, the run stops and names it"
        " (to clear it instead: metabot login set qxwork --on-sign-in clear)"
    ) in out
    assert "NAME" not in out.split("About logins")[0]


def test_login_add_says_clear_plainly(tmp_path, capsys):
    assert _add("qxwork", "claude", _folder(tmp_path, "w"), "--on-sign-in", "clear") == 0
    assert (
        "if another sign-in is set when a run starts: clear, MetaBot unsets it for this"
        " login's runs, and the receipt counts it"
    ) in capsys.readouterr().out
