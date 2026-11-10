"""What a login does about another sign-in that is set here (S4, PD-5 of 11-03).

A login is one of the person's own accounts, kept apart. When a variable that would sign
the engine in some other way is set (an API key, a token, a provider switch), the run on
the login would not be on the login alone. Each login says what happens then, and the
person chooses:

- ``refuse`` (the default): the run never starts, and the refusal names each variable and
  the one command that changes the choice. MetaBot never quietly changes which account
  pays.
- ``clear``: the person has chosen to let MetaBot drop those variables for the login's
  runs. The receipt's ``login`` verdict records the choice and how many it dropped.

The variables every run drops already (a parent Claude Code session's own) never cause a
refusal. ``login add`` prints the vendors' rules once, as facts with links.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import FAKE_CLI, FIXTURES
from metabot import commands
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, ConfigError, load_config, save_config
from metabot.engines import get_engine
from metabot.receipts import login_verdict, verify_file

ACCOUNT_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
PARENT_SESSION_VARS = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "logins" / name
    f.mkdir(parents=True)
    os.chmod(f, 0o700)
    return f


def _add(name: str, engine: str, home: Path, *more: str) -> int:
    return main(["login", "add", name, "--engine", engine, "--home", str(home), *more])


@pytest.fixture
def work(tmp_path: Path, capsys) -> Path:
    """A claude login ``qxwork``, added with the default choice."""
    f = _folder(tmp_path, "claude-work")
    assert _add("qxwork", "claude", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def side(tmp_path: Path, capsys) -> Path:
    """A codex login ``qxside``, added with the default choice."""
    f = _folder(tmp_path, "codex-side")
    assert _add("qxside", "codex", f) == 0
    capsys.readouterr()
    return f


def _set(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    for n in names:
        monkeypatch.setenv(n, f"secret-value-of-{n.lower()}")


def _entry(name: str) -> dict:
    return load_config().logins[name]


# ── the choice, as the CLI writes it ────────────────────────────────────────


def test_a_new_login_refuses_another_sign_in_unless_told_otherwise(work, capsys):
    assert _entry("qxwork")["on_sign_in"] == "refuse"
    assert main(["login", "list"]) == 0
    assert "another sign-in: refuse" in capsys.readouterr().out


def test_login_add_can_choose_clear(tmp_path, capsys):
    f = _folder(tmp_path, "claude-clear")
    assert _add("qxclear", "claude", f, "--on-sign-in", "clear") == 0
    assert _entry("qxclear")["on_sign_in"] == "clear"


def test_login_set_changes_the_choice_and_back(work, capsys):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    assert _entry("qxwork")["on_sign_in"] == "clear"
    assert "another sign-in: clear" in capsys.readouterr().out
    assert main(["login", "set", "qxwork", "--on-sign-in", "refuse"]) == 0
    assert _entry("qxwork")["on_sign_in"] == "refuse"


def test_login_set_changes_how_many_run_at_once(work):
    assert main(["login", "set", "qxwork", "--max-at-once", "3"]) == 0
    assert _entry("qxwork")["max_at_once"] == 3
    assert main(["login", "set", "qxwork", "--max-at-once", "9"]) == 2
    assert _entry("qxwork")["max_at_once"] == 3


def test_login_set_leaves_the_folder_and_engine_as_they_are(work):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    assert _entry("qxwork")["home"] == str(work)
    assert _entry("qxwork")["engine"] == "claude"


def test_login_set_needs_a_known_login_and_something_to_set(work, capsys):
    assert main(["login", "set", "qxnone", "--on-sign-in", "clear"]) == 2
    assert "no login 'qxnone'" in capsys.readouterr().err
    assert main(["login", "set", "qxwork"]) == 2
    assert "nothing to set" in capsys.readouterr().err


def test_an_unknown_choice_is_refused_by_the_cli(work):
    with pytest.raises(SystemExit) as stop:
        main(["login", "set", "qxwork", "--on-sign-in", "sometimes"])
    assert stop.value.code == 2


def test_config_with_an_unknown_choice_is_refused(tmp_path):
    f = _folder(tmp_path, "claude-bad")
    entry = {"engine": "claude", "home": str(f), "on_sign_in": "sometimes"}
    save_config(Config(logins={"qxbad": entry}))
    with pytest.raises(ConfigError):
        load_config()


def test_a_login_written_before_the_choice_existed_refuses(tmp_path):
    f = _folder(tmp_path, "claude-old")
    save_config(Config(logins={"qxold": {"engine": "claude", "home": str(f)}}))
    from metabot.config import login_of

    login = login_of(load_config(), "qxold")
    assert login is not None and login.on_sign_in == "refuse"


# ── the vendors' rules, at login add ────────────────────────────────────────


def test_login_add_prints_the_vendors_rules_as_facts_with_links(tmp_path, capsys):
    f = _folder(tmp_path, "claude-note")
    assert _add("qxnote", "claude", f) == 0
    out = capsys.readouterr().out
    assert "one of your own accounts" in out
    assert '"assume ordinary, individual usage"' in out
    assert "never use extra accounts to get around a plan's limits" in out.lower()
    assert "API key" in out
    assert "https://code.claude.com/docs/en/legal-and-compliance" in out
    assert "https://developers.openai.com/codex/auth" in out
    for line in ("more usage", "add up", "several subscriptions", "faster"):
        assert line not in out.lower()


def test_the_note_keeps_what_the_vendors_say_apart_from_metabot_s_rules(tmp_path, capsys):
    f = _folder(tmp_path, "claude-note2")
    assert _add("qxnote", "claude", f) == 0
    out = capsys.readouterr().out
    vendors, ours = out.split("What the vendors' pages say:", 1)[1].split("MetaBot's rules:", 1)
    # each vendor line says only what that vendor's page says
    said = " ".join(vendors.split())
    assert "With file storage, codex keeps its sign-in in auth.json under CODEX_HOME." in said
    assert "one folder per login" not in out
    assert "never use extra accounts" not in vendors.lower()
    # MetaBot's own behaviour and rules, said as ours
    ours = " ".join(ours.split())
    assert "MetaBot gives each login its own folder" in ours
    assert "Never use extra accounts to get around a plan's limits." in ours
    assert "For unattended runs, such as schedules, use an API key." in ours


def test_a_refused_login_add_prints_no_note(tmp_path, capsys):
    assert _add("qxnohome", "claude", tmp_path / "not-there") == 2
    assert "one of your own accounts" not in capsys.readouterr().out


# ── refuse: the run never starts ────────────────────────────────────────────


def test_refuse_names_each_variable_and_the_one_command(work, monkeypatch):
    _set(monkeypatch, ACCOUNT_VARS)
    with pytest.raises(ValueError) as refused:
        get_engine("claude", login="qxwork")
    said = str(refused.value)
    assert all(n in said for n in ACCOUNT_VARS)
    assert "metabot login set qxwork --on-sign-in clear" in said
    assert "secret-value" not in said


def test_refuse_holds_for_codex_too(side, monkeypatch):
    _set(monkeypatch, ("OPENAI_API_KEY",))
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        get_engine("codex", login="qxside")


def test_with_nothing_else_set_a_refusing_login_runs(work, monkeypatch):
    for n in ACCOUNT_VARS:
        monkeypatch.delenv(n, raising=False)
    engine = get_engine("claude", login="qxwork")
    assert engine.login.name == "qxwork"
    assert engine.cleared == ()


def test_a_parent_session_s_own_variables_never_cause_a_refusal(work, monkeypatch):
    _set(monkeypatch, PARENT_SESSION_VARS)
    engine = get_engine("claude", login="qxwork")
    assert engine.cleared == ()
    env = engine.env()
    assert [n for n in PARENT_SESSION_VARS if n in env] == []


def test_without_a_login_nothing_is_refused(work, monkeypatch, claude_config):
    _set(monkeypatch, ACCOUNT_VARS)
    env = get_engine("claude").env()
    assert all(n in env for n in ACCOUNT_VARS)


# ── clear: dropped for the run, and counted ─────────────────────────────────


def test_clear_drops_each_variable_and_counts_them(work, monkeypatch):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    _set(monkeypatch, ACCOUNT_VARS)
    engine = get_engine("claude", login="qxwork")
    assert engine.cleared == tuple(sorted(ACCOUNT_VARS))
    env = engine.env()
    assert [n for n in ACCOUNT_VARS if n in env] == []
    assert env["CLAUDE_CONFIG_DIR"] == str(work)


@pytest.mark.parametrize(
    ("choice", "cleared", "detail"),
    [
        ("refuse", 0, "engine=claude;key_source=none;on_sign_in=refuse;cleared=0"),
        ("clear", 2, "engine=claude;key_source=none;on_sign_in=clear;cleared=2"),
    ],
)
def test_the_login_verdict_records_the_choice_and_the_count(choice, cleared, detail):
    verdict = login_verdict("claude", "none", choice, cleared)
    assert verdict == {"check": "login", "result": "pass", "detail": detail}


# ── ask --login ─────────────────────────────────────────────────────────────


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


def _receipt_verdicts(err: str) -> list[dict]:
    line = next(x for x in err.splitlines() if x.startswith("[metabot] receipt "))
    path = Path(line.split()[2])
    assert verify_file(path).valid is True
    return json.loads(path.read_text())["verdicts"]


def test_ask_on_a_login_runs_on_its_folder(work, fake_claude, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    code = main(["ask", "-e", "claude", "--login", "qxwork", "--cwd", str(project), "hi"])
    assert code == 0
    assert json.loads(fake_claude.read_text())["claude_config_dir"] == str(work)
    verdicts = _receipt_verdicts(capsys.readouterr().err)
    assert {
        "check": "login",
        "result": "pass",
        "detail": "engine=claude;key_source=none;on_sign_in=refuse;cleared=0",
    } in verdicts


def test_ask_on_a_refusing_login_with_a_key_set_never_starts(
    work, fake_claude, tmp_path, monkeypatch, capsys
):
    _set(monkeypatch, ("ANTHROPIC_API_KEY",))
    code = main(["ask", "-e", "claude", "--login", "qxwork", "--cwd", str(tmp_path), "hi"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "--on-sign-in clear" in err
    assert not fake_claude.exists()


def test_ask_on_a_clearing_login_says_what_it_cleared(
    work, fake_claude, tmp_path, monkeypatch, capsys
):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    _set(monkeypatch, ACCOUNT_VARS)
    code = main(["ask", "-e", "claude", "--login", "qxwork", "--cwd", str(tmp_path), "hi"])
    assert code == 0
    started = json.loads(fake_claude.read_text())
    assert [n for n in ACCOUNT_VARS if n in started["env_keys"]] == []
    err = capsys.readouterr().err
    assert "cleared 2 other sign-in variables" in err
    assert "on_sign_in=clear;cleared=2" in json.dumps(_receipt_verdicts(err))


def test_ask_with_a_login_of_another_engine_never_starts(
    work, fake_claude, tmp_path, monkeypatch, capsys
):
    code = main(["ask", "-e", "codex", "--login", "qxwork", "--cwd", str(tmp_path), "hi"])
    assert code == 2
    assert "qxwork is a claude login, not one for codex" in capsys.readouterr().err


def test_ask_with_an_unknown_login_never_starts(fake_claude, tmp_path, capsys):
    save_config(Config())
    code = main(["ask", "-e", "claude", "--login", "qxnone", "--cwd", str(tmp_path), "hi"])
    assert code == 2
    assert "no login named 'qxnone'" in capsys.readouterr().err
    assert not fake_claude.exists()


def test_ask_login_and_ladder_do_not_go_together(work, tmp_path, capsys):
    code = main(["ask", "--ladder", "--login", "qxwork", "--cwd", str(tmp_path), "hi"])
    assert code == 2
    assert "--login" in capsys.readouterr().err
