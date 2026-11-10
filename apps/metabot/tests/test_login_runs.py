"""Runs on a login (S3): the engine starts on the login's folder, and nothing else signs it in.

A login names one of the person's own claude or codex sign-in folders (S1). A run on it
sets the engine's folder variable to that folder and drops every variable the CLI would
sign in with instead, or send its calls elsewhere with. MetaBot never opens a file in the
folder: the person's settings there are not carried. The receipt signs a ``login``
verdict, and pins the login's name and folder by salted digest, so a receipt that
travels names neither.
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.engines import get_engine, with_login
from metabot.engines.base import EngineJob
from metabot.engines.claude import ClaudeEngine
from metabot.events import Started
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.receipts import journal_matches, login_verdict, verify_file
from metabot.runner import run_job

# Names claude 2.1.317 reads to sign in, to pick a provider, or to send its calls elsewhere
# (read from the installed CLI's own text, 2026-11-03).
CLAUDE_SIGN_IN = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_UNIX_SOCKET",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_GATEWAY_TOKEN",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_HOST_CREDS_FILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
)
# Names it reads that neither sign in nor send calls elsewhere: kept.
CLAUDE_KEPT = (
    "CLAUDE_CODE_USE_POWERSHELL_TOOL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "DISABLE_TELEMETRY",
    "A_VARIABLE_OF_THEIRS",
)
# codex 0.154.0, read the same way.
CODEX_SIGN_IN = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
    "CODEX_SQLITE_HOME",
)
CODEX_KEPT = (
    "CODEX_CA_CERTIFICATE",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "A_VARIABLE_OF_THEIRS",
    "CODEX_KEYBINDINGS",  # not a codex name: a word inside a word is not the word KEY
)


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "logins" / name
    f.mkdir(parents=True)
    os.chmod(f, 0o700)
    return f


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A claude login named ``qxwork``: a name no receipt text holds by chance."""
    f = _folder(tmp_path, "claude-work")
    assert main(["login", "add", "qxwork", "--engine", "claude", "--home", str(f)]) == 0
    return f


@pytest.fixture
def side(tmp_path: Path) -> Path:
    """A codex login named ``qxside``."""
    f = _folder(tmp_path, "codex-side")
    assert main(["login", "add", "qxside", "--engine", "codex", "--home", str(f)]) == 0
    return f


def _set(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    for n in names:
        monkeypatch.setenv(n, f"value-of-{n.lower()}")


# ── the engine's environment ────────────────────────────────────────────────


def test_a_claude_login_points_the_engine_at_its_folder(work):
    assert get_engine("claude", login="qxwork").env()["CLAUDE_CONFIG_DIR"] == str(work)


def test_a_codex_login_points_the_engine_at_its_folder(side):
    assert get_engine("codex", login="qxside").env()["CODEX_HOME"] == str(side)


def test_on_a_claude_login_every_other_way_to_sign_in_is_dropped(work, monkeypatch):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    _set(monkeypatch, CLAUDE_SIGN_IN + CLAUDE_KEPT)
    env = get_engine("claude", login="qxwork").env()
    assert [n for n in CLAUDE_SIGN_IN if n in env] == []
    assert all(n in env for n in CLAUDE_KEPT)


def test_on_a_codex_login_every_other_way_to_sign_in_is_dropped(side, monkeypatch):
    assert main(["login", "set", "qxside", "--on-sign-in", "clear"]) == 0
    _set(monkeypatch, CODEX_SIGN_IN + CODEX_KEPT)
    env = get_engine("codex", login="qxside").env()
    assert [n for n in CODEX_SIGN_IN if n in env] == []
    assert all(n in env for n in CODEX_KEPT)


def test_without_a_login_the_environment_is_as_before(monkeypatch, claude_config):
    _set(monkeypatch, CLAUDE_SIGN_IN)
    env = get_engine("claude").env()
    assert all(n in env for n in CLAUDE_SIGN_IN)
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_config)


def test_the_default_login_is_the_environment_s_own_sign_in(monkeypatch, claude_config):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "theirs")
    engine = get_engine("claude", login="default")
    assert engine.login is None
    assert engine.env()["ANTHROPIC_API_KEY"] == "theirs"


def test_a_codex_login_hides_what_its_own_folder_holds(side, tmp_path):
    (side / "AGENTS.md").write_text("theirs\n")
    engine = get_engine("codex", login="qxside")
    engine.config.codex_ignore_user_config = True
    assert side / "AGENTS.md" in engine.user_instructions(tmp_path)


# ── refusals ────────────────────────────────────────────────────────────────


def test_a_login_for_another_engine_is_refused(work):
    with pytest.raises(ValueError, match="qxwork is a claude login"):
        get_engine("codex", login="qxwork")


@pytest.mark.parametrize("engine", ["cursor", "openai"])
def test_an_engine_with_no_logins_refuses_one(work, engine):
    with pytest.raises(ValueError, match=f"{engine} takes no login"):
        get_engine(engine, login="qxwork")


def test_a_login_that_is_not_there_is_refused(work):
    with pytest.raises(ValueError, match="no login named 'home'"):
        get_engine("claude", login="home")


def test_a_folder_others_can_enter_now_stops_the_run_before_it_starts(work):
    os.chmod(work, 0o755)
    with pytest.raises(ValueError, match="others can"):
        get_engine("claude", login="qxwork")


def test_a_folder_that_went_away_stops_the_run_before_it_starts(work):
    work.rmdir()
    with pytest.raises(ValueError, match="not there"):
        get_engine("claude", login="qxwork")


def test_a_folder_a_project_grew_around_stops_the_run_before_it_starts(work):
    (work.parent / ".git" / "objects").mkdir(parents=True)
    (work.parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(ValueError, match="inside the project"):
        get_engine("claude", login="qxwork")


def test_with_login_takes_an_engine_already_made(work):
    engine = with_login(wrap(ClaudeEngine), "qxwork")
    assert engine.login is not None and engine.login.name == "qxwork"
    assert engine.env()["CLAUDE_CONFIG_DIR"] == str(work)


# ── nothing in the folder is opened ─────────────────────────────────────────


def test_the_settings_in_a_login_folder_are_never_opened(work, tmp_path, monkeypatch):
    (work / "settings.json").write_text(
        json.dumps({"model": "theirs", "permissions": {"deny": ["Read(x)"]}})
    )
    opened: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy(path, *a, **k):
        opened.append(os.fspath(path))
        return real_open(path, *a, **k)

    def os_spy(path, *a, **k):
        opened.append(os.fspath(path))
        return real_os_open(path, *a, **k)

    engine = get_engine("claude", login="qxwork")
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(os, "open", os_spy)
    argv = engine.argv(EngineJob(prompt="hi", cwd=tmp_path))
    assert "--settings" not in argv
    assert [p for p in opened if p.startswith(str(work))] == []


def test_the_run_s_own_hook_still_rides_in_the_settings(work, tmp_path):
    engine = get_engine("claude", login="qxwork")
    argv = engine.argv(EngineJob(prompt="hi", cwd=tmp_path, gate_hook=["metabot-hook"]))
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert set(settings) == {"hooks"}


# ── what the engine says it signed in with ──────────────────────────────────


def test_claude_s_start_says_how_it_signed_in():
    state: dict = {}
    line = (FIXTURES / "claude-echo.jsonl").read_text().splitlines()[0]
    (started,) = ClaudeEngine().parse_line(line, state)
    assert isinstance(started, Started)
    assert started.key_source == "none"


CHOSE = ";on_sign_in=refuse;cleared=0"


@pytest.mark.parametrize(
    ("engine", "source", "result", "detail"),
    [
        ("claude", "none", "pass", "engine=claude;key_source=none" + CHOSE),
        ("codex", None, "pass", "engine=codex;key_source=unreported" + CHOSE),
        (
            "claude",
            "ANTHROPIC_API_KEY",
            "fail",
            "engine=claude;key_source=ANTHROPIC_API_KEY" + CHOSE,
        ),
        ("claude", "apiKeyHelper;x=y", "fail", "engine=claude;key_source=apiKeyHelper_x=y" + CHOSE),
    ],
)
def test_the_login_verdict(engine, source, result, detail):
    verdict = login_verdict(engine, source, "refuse", 0)
    assert verdict == {"check": "login", "result": result, "detail": detail}


# ── the run and its receipt ─────────────────────────────────────────────────


async def _run(tmp_path, engine):
    return await run_job(
        engine=engine,
        prompt="say metabot-probe",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
    )


async def test_a_run_on_a_login_starts_the_engine_on_its_folder_alone(work, tmp_path, monkeypatch):
    assert main(["login", "set", "qxwork", "--on-sign-in", "clear"]) == 0
    _set(monkeypatch, CLAUDE_SIGN_IN)
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(seen))
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path, with_login(wrap(ClaudeEngine), "qxwork"))
    assert result.ok is True, result.error
    started = json.loads(seen.read_text())
    assert started["claude_config_dir"] == str(work)
    assert [n for n in CLAUDE_SIGN_IN if n in started["env_keys"]] == []


async def test_a_run_on_a_login_signs_a_login_verdict_and_pins_name_and_folder(
    work, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path, with_login(wrap(ClaudeEngine), "qxwork"))
    receipt_text = result.receipt.read_text()
    envelope = json.loads(receipt_text)
    assert {
        "check": "login",
        "result": "pass",
        "detail": "engine=claude;key_source=none;on_sign_in=refuse;cleared=0",
    } in envelope["verdicts"]
    (content,) = [v for v in envelope["verdicts"] if v["check"] == "content"]
    pinned = dict(part.split("=", 1) for part in content["detail"].split(";"))
    assert {"login", "login_folder"} <= set(pinned)
    assert str(work) not in receipt_text and "qxwork" not in receipt_text
    journal = json.loads((run_dir(result.run_id) / "journal.json").read_text())
    assert journal["meta"]["login"] == "qxwork"
    assert journal["meta"]["login_folder"] == str(work)
    assert verify_file(result.receipt).valid is True
    assert journal_matches(result.run_id) is True


async def test_an_engine_that_signed_in_another_way_fails_the_login_verdict(
    work, tmp_path, monkeypatch
):
    lines = (FIXTURES / "claude-echo.jsonl").read_text().splitlines()
    init = json.loads(lines[0])
    init["apiKeySource"] = "ANTHROPIC_API_KEY"
    fixture = tmp_path / "keyed.jsonl"
    fixture.write_text("\n".join([json.dumps(init), *lines[1:]]) + "\n")
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    result = await _run(tmp_path, with_login(wrap(ClaudeEngine), "qxwork"))
    envelope = json.loads(result.receipt.read_text())
    assert {
        "check": "login",
        "result": "fail",
        "detail": "engine=claude;key_source=ANTHROPIC_API_KEY;on_sign_in=refuse;cleared=0",
    } in envelope["verdicts"]


async def test_a_run_with_no_login_signs_no_login_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = await _run(tmp_path, wrap(ClaudeEngine))
    envelope = json.loads(result.receipt.read_text())
    assert [v for v in envelope["verdicts"] if v["check"] == "login"] == []
    journal = json.loads((run_dir(result.run_id) / "journal.json").read_text())
    assert "login" not in journal["meta"] and "login_folder" not in journal["meta"]
