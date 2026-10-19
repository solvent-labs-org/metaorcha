"""The command line, end to end, against stand-in engine CLIs on a private PATH."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FAKE_CLI, FAKE_MCP, FIXTURES
from metabot import commands
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, load_config, save_config
from metabot.connections import ConnectionStore
from metabot.console import Trace
from metabot.paths import receipts_dir, run_dir
from metabot.repl import repl
from metabot.runner import RunCancelled


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH holding only a ``claude`` that is the fake CLI (no real engine is found)."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    save_config(Config(openai_base_url="http://127.0.0.1:9/v1"))
    return d


@pytest.fixture
def echo(bin_dir, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    return bin_dir


def _add_fake_server(name: str = "gh", token: str = "tok-123") -> None:
    ConnectionStore().add(
        name=name,
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP)],
        token=token,
        token_env="FAKE_TOKEN",
    )


# ── init / engines ──────────────────────────────────────────────────────────


def test_init_writes_key_and_config(bin_dir, tmp_path, capsys):
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert (tmp_path / "orcha-key").exists()
    assert load_config().default_engine == "claude"
    assert "signer    did:orcha:agent:key-" in out
    assert "engine    claude" in out


# three bytes, an empty file, bytes that are not text, and a seed that is too short
BROKEN_KEYS = [b"abc", b"", b"\xff\xfe\n", b"QUJD\n"]


@pytest.mark.parametrize("raw", BROKEN_KEYS)
def test_a_key_file_that_does_not_read_is_named_and_left_alone(
    raw, echo, tmp_path, home, capsys, monkeypatch
):
    monkeypatch.setattr("metabot.receipts.KEY_WAIT_S", 0.05)
    key = tmp_path / "orcha-key"
    key.write_bytes(raw)
    for argv in (["init"], ["ask", "--cwd", str(tmp_path), "hi"]):
        assert main(argv) == 1, argv
        err = capsys.readouterr().err
        assert err.startswith(f"metabot: cannot read the signing key {key}: "), (argv, err)
    assert main(["doctor"]) == 1
    assert f"key      {key} (does not read: " in capsys.readouterr().out
    # never replaced: the receipts it signed would then name a signer nobody holds
    assert key.read_bytes() == raw
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())  # no run begun


def test_a_key_path_that_is_a_folder_is_named(echo, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("metabot.receipts.KEY_WAIT_S", 0.05)
    key = tmp_path / "orcha-key"
    key.mkdir()
    assert main(["ask", "--cwd", str(tmp_path), "hi"]) == 1
    said = capsys.readouterr().err
    assert said == f"metabot: cannot read the signing key {key}: it is not a file\n"


def test_doctor_says_a_missing_key_is_made_on_first_use(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", "/no/such/bin")  # doctor asks each engine CLI it finds its version
    assert main(["doctor"]) == 0
    assert (
        f"key      {tmp_path / 'orcha-key'} (missing: run metabot init)" in capsys.readouterr().out
    )


def test_doctor_names_the_command_a_bundled_plugin_calls(monkeypatch, capsys):
    from test_needs import _plugin

    monkeypatch.setattr("metabot.needs.enabled_plugins", lambda: [_plugin("code-desks")])
    monkeypatch.setenv("PATH", "/no/such/bin")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "plugin   code-desks  needs desk: missing: it comes with Code Desks" in out


def test_doctor_says_an_installed_plugin_changed_and_fails(monkeypatch, capsys):
    from metabot.plugins import PluginError

    def changed():
        raise PluginError("tools@local's installed copy changed since it was installed")

    monkeypatch.setattr("metabot.needs.enabled_plugins", changed)
    monkeypatch.setenv("PATH", "/no/such/bin")
    assert main(["doctor"]) == 1  # no run that takes plugins can start
    out = capsys.readouterr().out
    assert "plugins  tools@local's installed copy changed since it was installed" in out
    assert "engine   claude" in out  # the other checks still ran


def test_init_without_any_cli_picks_the_local_endpoint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert main(["init"]) == 0
    assert load_config().default_engine == "openai"


def test_init_keeps_an_earlier_choice(bin_dir):
    save_config(Config(default_engine="codex"))
    assert main(["init"]) == 0
    assert load_config().default_engine == "codex"


def test_engines_marks_the_default_and_never_contacts_a_remote(bin_dir, capsys):
    assert main(["engines"]) == 0
    out = capsys.readouterr().out
    assert f"* claude  {bin_dir / 'claude'}" in out
    assert "codex   not installed (codex not on PATH)" in out
    assert "openai  http://127.0.0.1:9/v1 model qwen2.5:3b: not reachable" in out
    save_config(Config(openai_base_url="https://models.example.invalid/v1"))
    main(["engines"])
    assert "configured (remote; not checked)" in capsys.readouterr().out


# ── ask ─────────────────────────────────────────────────────────────────────


def test_ask_prints_final_and_receipt_path(echo, tmp_path, capsys):
    assert main(["ask", "--cwd", str(tmp_path), "say", "metabot-probe"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "metabot-probe\n"
    assert "[metabot] receipt " in captured.err
    assert "(valid, 1 steps)" in captured.err
    assert "  - Bash  echo metabot-probe" in captured.err


def test_ask_json(echo, tmp_path, capsys):
    assert main(["ask", "--json", "--cwd", str(tmp_path), "hi"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True and result["valid"] is True
    assert result["final_text"] == "metabot-probe"
    assert Path(result["receipt"]).is_file()


def test_ask_reads_the_prompt_from_stdin(echo, tmp_path, monkeypatch, capsys):
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    monkeypatch.setattr(sys, "stdin", io.StringIO("a prompt from stdin\n"))
    assert main(["ask", "-q", "--cwd", str(tmp_path), "-"]) == 0
    argv = json.loads(argv_out.read_text())["argv"]
    assert argv[-2:] == ["--", "a prompt from stdin\n"]
    assert capsys.readouterr().err == ""  # -q: no trace and no receipt line


def test_ask_failure_still_seals(bin_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-error-result.jsonl"))
    assert main(["ask", "--cwd", str(tmp_path), "go"]) == 1
    assert "(valid, 0 steps)" in capsys.readouterr().err


def test_ask_rejects_an_unknown_engine_and_a_missing_dir(echo, tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["ask", "-e", "nope", "hi"])
    assert exit_info.value.code == 2
    assert main(["ask", "--cwd", str(tmp_path / "missing"), "hi"]) == 2


def test_ask_allow_resolves_connection_and_engine_tools(bin_dir):
    _add_fake_server()
    store = ConnectionStore()
    engine = SimpleNamespace(tool_prefix="claude-code")
    allowed = commands.resolve_allows(
        ["gh__create_issue", "Edit", "did:orcha:agent:x#y", "nope__thing"], engine, store
    )
    assert allowed == {
        f"{store.get('gh').id}#create_issue",
        "claude-code/Edit",
        "did:orcha:agent:x#y",
        "claude-code/nope__thing",
    }


CAVEAT = "The answer may say otherwise; the receipt's steps are what happened."


@pytest.mark.parametrize("quiet", [[], ["-q"]])
def test_ask_says_when_a_call_was_declined_even_when_quiet(
    echo, tmp_path, monkeypatch, capsys, quiet
):
    ask = {"op": "approve", "call_id": "toolu_x", "tool": "claude-code/Write", "cls": "write"}
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    assert main(["ask", *quiet, "--cwd", str(tmp_path), "write it"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "metabot-probe\n"  # the answer stays as it was
    assert f"[metabot] 1 call was declined. {CAVEAT}" in captured.err.splitlines()


def test_ask_says_nothing_more_when_every_call_went_as_asked(echo, tmp_path, capsys):
    assert main(["ask", "--cwd", str(tmp_path), "say", "it"]) == 0
    assert "may say otherwise" not in capsys.readouterr().err


UNREAD = (
    "[metabot] calc.py is here, but this run made no tool calls,"
    " so the receipt shows no read of it."
)


def test_ask_says_when_it_answered_about_a_file_here_without_a_tool_call(
    bin_dir, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-answer-only.jsonl"))
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    ask = ["ask", "-q", "--cwd", str(tmp_path)]
    assert main([*ask, "what does calc.py do?"]) == 0
    assert UNREAD in capsys.readouterr().err.splitlines()  # about the answer, so said when quiet
    assert main([*ask, "what is two plus two?"]) == 0
    assert "made no tool calls" not in capsys.readouterr().err
    assert main([*ask, "--resume", "s-1", "what does calc.py do?"]) == 0
    assert "made no tool calls" not in capsys.readouterr().err  # an earlier turn may have read it
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    assert main([*ask, "what does calc.py do?"]) == 0
    assert "made no tool calls" not in capsys.readouterr().err


def test_chat_says_so_on_a_new_conversation_and_not_on_a_resumed_one(
    bin_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-answer-only.jsonl"))
    (tmp_path / "calc.py").write_text("")
    out: list[str] = []
    trace = Trace(out=io.StringIO())
    script = Script("what does calc.py do?", "and calc.py again?", "/new", "calc.py?")
    assert repl(script, out.append, engine="claude", cwd=tmp_path, trace=trace) == 0
    assert trace.out.getvalue().splitlines().count(UNREAD) == 2  # the first turn, and after /new


def test_ask_json_counts_the_declined_and_failed_calls(echo, tmp_path, monkeypatch, capsys):
    ask = {"op": "approve", "call_id": "toolu_x", "tool": "claude-code/Write", "cls": "write"}
    monkeypatch.setenv("FAKE_CONTROL", "from-hook")
    monkeypatch.setenv("FAKE_CONTROL_MSG", json.dumps(ask))
    assert main(["ask", "--json", "--cwd", str(tmp_path), "write it"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["declined"], result["failed"]) == (1, 0)


def test_ask_cancelled_reports_the_receipt(bin_dir, tmp_path, monkeypatch, capsys):
    sealed = SimpleNamespace(receipt=tmp_path / "r.json", session_id=None)

    async def cancelled(**_kw):
        raise RunCancelled(sealed)

    monkeypatch.setattr(commands, "ask_once", cancelled)
    assert main(["ask", "--cwd", str(tmp_path), "go"]) == 130
    assert f"[metabot] cancelled; receipt {tmp_path / 'r.json'}" in capsys.readouterr().err


def test_cancelled_result_survives_ctrl_c():
    sealed = SimpleNamespace(receipt="r")
    try:
        try:
            raise RunCancelled(sealed)
        except asyncio.CancelledError:
            raise KeyboardInterrupt from None
    except KeyboardInterrupt as exc:
        assert exc.__context__ is not None  # `from None` hides, but keeps, the context
        assert commands.cancelled_result(exc) is sealed
    assert commands.cancelled_result(KeyboardInterrupt()) is None


# ── connect ─────────────────────────────────────────────────────────────────


def test_connect_add_reads_token_from_stdin_not_argv(bin_dir, monkeypatch, capsys, home):
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok-from-stdin\n"))
    argv = ["connect", "add", "gh", "--token-env", "FAKE_TOKEN", "--command"]
    assert main([*argv, sys.executable, str(FAKE_MCP), "--count-file", "x"]) == 0
    conn = ConnectionStore().get("gh")
    assert conn.command == [sys.executable, str(FAKE_MCP), "--count-file", "x"]
    assert ConnectionStore().secret(conn) == "tok-from-stdin"
    assert "tok-from-stdin" not in (home / "connections.json").read_text()
    assert "tok-from-stdin" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", [["--token", "sekrit"], ["--token=sekrit"], ["--tok", "sekrit"]])
def test_a_token_on_the_command_line_is_refused_unechoed(bin_dir, capsys, flag):
    assert main(["connect", "add", "gh", *flag, "--url", "https://x.example/mcp"]) == 2
    captured = capsys.readouterr()
    assert "never taken on the command line" in captured.err
    assert "sekrit" not in captured.err + captured.out
    assert ConnectionStore().list() == []


def test_connect_add_needs_one_target(bin_dir, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["connect", "add", "gh", "--no-token", "--command"]) == 2
    with pytest.raises(SystemExit):
        main(["connect", "add", "gh", "--url", "https://a", "--command", "x"])


def test_connect_add_reports_a_bad_name(bin_dir, capsys):
    assert main(["connect", "add", "Bad_Name", "--no-token", "--url", "https://x"]) == 2
    assert "a name is 1-32" in capsys.readouterr().err


def test_connect_scope_lists_classes(bin_dir, capsys):
    _add_fake_server()
    assert main(["connect", "scope", "gh"]) == 0
    rows = dict(line.split(None, 1) for line in capsys.readouterr().out.splitlines())
    assert rows["list_issues"].strip() == "read"
    assert rows["create_issue"].strip() == "write"
    assert rows["delete_repo"].strip() == "destructive"
    assert rows["frobnicate"].strip() == "destructive"
    assert main(["connect", "scope", "gh", "--set", "list_issues=write"]) == 0
    out = capsys.readouterr().out
    assert "list_issues                      write  (you set write)" in out
    assert "can only tighten" in out
    assert main(["connect", "scope", "gh", "--set", "list_issues=sideways"]) == 2


def test_connect_scope_shows_a_setting_for_a_tool_the_server_does_not_list(bin_dir, capsys):
    """A misspelt name tightens nothing, so the person sees it rather than trusting it."""
    _add_fake_server()
    assert main(["connect", "scope", "gh", "--set", "delete_rep=destructive"]) == 0
    out = capsys.readouterr().out
    said = "destructive  (you set it; gh lists no such tool)"
    assert f"delete_rep                       {said}" in out
    assert "delete_repo                      destructive\n" in out  # the real one, as it was


def test_connect_list_test_and_revoke(bin_dir, home, capsys):
    _add_fake_server()
    conn = ConnectionStore().get("gh")
    assert main(["connect", "list"]) == 0
    assert f"gh               active   stdio {conn.id}" in capsys.readouterr().out
    assert main(["connect", "test", "gh"]) == 0
    assert "gh: 5 tools: list_issues" in capsys.readouterr().out
    assert main(["connect", "revoke", "gh"]) == 0
    assert "any token it had is deleted" in capsys.readouterr().out
    assert not (home / "secrets" / conn.suffix).exists()
    assert main(["connect", "test", "gh"]) == 2
    assert "gh is revoked" in capsys.readouterr().err
    assert main(["connect", "test", "nobody"]) == 2


def test_connect_test_shows_what_a_failing_server_said_redacted(bin_dir, tmp_path, capsys):
    server = tmp_path / "bad_server.py"
    server.write_text(
        "import os, sys\n"
        "print('starting with', os.environ.get('FAKE_TOKEN'), file=sys.stderr)\n"
        "print('fatal: no database', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    ConnectionStore().add(
        name="bad",
        transport="stdio",
        command=[sys.executable, str(server)],
        token="tok-abc",
        token_env="FAKE_TOKEN",
    )
    assert main(["connect", "test", "bad"]) == 1
    err = capsys.readouterr().err
    assert "bad did not answer" in err
    assert "  bad: fatal: no database" in err
    assert "tok-abc" not in err
    assert "  bad: starting with [REDACTED:FAKE_TOKEN]" in err


def test_connect_test_reports_a_dead_server(bin_dir, tmp_path, capsys):
    ConnectionStore().add(name="gone", transport="stdio", command=[str(tmp_path / "no-server")])
    assert main(["connect", "test", "gone"]) == 1
    assert "gone did not answer" in capsys.readouterr().err


# ── receipts ────────────────────────────────────────────────────────────────


def _one_run(tmp_path, capsys) -> str:
    assert main(["ask", "--json", "--cwd", str(tmp_path), "hi"]) == 0
    return json.loads(capsys.readouterr().out)["run_id"]


def test_receipts_verify_exit_codes(echo, tmp_path, capsys):
    run_id = _one_run(tmp_path, capsys)
    assert main(["receipts", "verify", run_id]) == 0
    out = capsys.readouterr().out
    assert out.startswith("VALID")
    assert "journal re-derives every hash" in out
    receipt = receipts_dir() / f"{run_id}.json"
    assert main(["receipts", "verify", str(receipt)]) == 0
    capsys.readouterr()

    journal_path = run_dir(run_id) / "journal.json"
    journal = json.loads(journal_path.read_text())
    journal["steps"][0]["output"] = "something else"
    journal_path.write_text(json.dumps(journal))
    assert main(["receipts", "verify", run_id]) == 1
    out = capsys.readouterr().out
    assert out.startswith("INVALID")  # the first line is the verdict the exit code gives
    assert "signature ok" in out and "journal DOES NOT MATCH the receipt" in out

    text = receipt.read_text()
    receipt.write_text(text.replace('"success": true', '"success": false', 1))
    assert main(["receipts", "verify", run_id]) == 1
    assert capsys.readouterr().out.startswith("INVALID")
    assert main(["receipts", "verify", "no-such-run"]) == 2
    assert main(["receipts", "verify", "../../no-such/receipt"]) == 2


def test_receipts_verify_creates_nothing_in_the_metabot_home(echo, tmp_path, capsys, monkeypatch):
    run_id = _one_run(tmp_path, capsys)
    shared = tmp_path / "shared.json"
    shared.write_bytes((receipts_dir() / f"{run_id}.json").read_bytes())
    fresh = tmp_path / "fresh-home"
    monkeypatch.setenv("METABOT_HOME", str(fresh))
    assert main(["receipts", "verify", str(shared)]) == 0
    assert main(["receipts", "verify", run_id]) == 2  # not kept in this home
    capsys.readouterr()
    assert not fresh.exists()  # a check reads: it makes no home and no receipts folder


def test_receipts_list_and_show(echo, tmp_path, capsys):
    assert main(["receipts", "list"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    run_id = _one_run(tmp_path, capsys)
    assert main(["receipts", "list"]) == 0
    listed = capsys.readouterr().out
    assert run_id in listed and "claude" in listed and "ok" in listed
    assert main(["receipts", "show", run_id]) == 0
    shown = capsys.readouterr().out
    assert "ok   claude-code/Bash" in shown
    assert "verdict model: pass claude/claude-haiku-5-5" in shown
    assert "answer    metabot-probe" in shown
    assert main(["receipts", "show", "nope"]) == 2


def test_receipts_list_takes_the_last_n_and_none_for_a_limit_below_one(echo, tmp_path, capsys):
    first, second = _one_run(tmp_path, capsys), _one_run(tmp_path, capsys)
    assert main(["receipts", "list", "-n", "1"]) == 0
    listed = capsys.readouterr().out
    assert second in listed and first not in listed
    for limit in ("0", "-1"):
        assert main(["receipts", "list", "-n", limit]) == 0
        listed = capsys.readouterr().out
        assert first not in listed and second not in listed


def test_looking_up_a_run_never_creates_its_folder(home, capsys):
    assert main(["receipts", "show", "mb-typo"]) == 2
    assert "no run 'mb-typo'" in capsys.readouterr().err
    assert not (home / "runs" / "mb-typo").exists()


def test_a_receipt_whose_journal_is_gone_does_not_match(echo, tmp_path, home, capsys):
    import shutil

    run_id = _one_run(tmp_path, capsys)
    shutil.rmtree(home / "runs" / run_id)
    assert main(["receipts", "verify", run_id]) == 1
    assert "DOES NOT MATCH" in capsys.readouterr().out
    assert not (home / "runs" / run_id).exists()


def test_a_journal_edited_into_another_shape_does_not_match(echo, tmp_path, home, capsys):
    run_id = _one_run(tmp_path, capsys)
    journal = home / "runs" / run_id / "journal.json"
    data = json.loads(journal.read_text(encoding="utf-8"))
    data["steps"] = {str(n): n for n in range(len(data["steps"]))}
    journal.write_text(json.dumps(data), encoding="utf-8")
    assert main(["receipts", "verify", run_id]) == 1
    assert "DOES NOT MATCH" in capsys.readouterr().out


# ── what reaches a terminal ─────────────────────────────────────────────────

# a window title, a screen clear, a carriage return, a C1 CSI and a bidi override
EVIL = "a\x1b]0;owned\x07\x1b[2Jb\rc\x9b31m\u202eevil"
RAW = "\x1b\x07\r\x9b\u202e"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _assert_no_controls(text: str) -> None:
    assert not [ch for ch in RAW if ch in text], repr(text)


def _evil_answer(tmp_path, monkeypatch) -> str:
    lines = (FIXTURES / "claude-echo.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["result"] = "first line\n\tindented " + EVIL
    path = tmp_path / "evil.jsonl"
    path.write_text("\n".join([*lines[:-1], json.dumps(last)]) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_FIXTURE", str(path))
    return last["result"]


def test_an_answer_shown_on_a_terminal_cannot_repaint_it(bin_dir, tmp_path, monkeypatch):
    _evil_answer(tmp_path, monkeypatch)
    tty = _Tty()
    monkeypatch.setattr(sys, "stdout", tty)
    assert main(["ask", "--cwd", str(tmp_path), "hi"]) == 0
    out = tty.getvalue()
    _assert_no_controls(out)
    assert "first line\n\tindented a\\x1b]0;owned\\x07" in out  # lines and tabs stay


def test_an_answer_sent_to_a_pipe_is_kept_exactly(bin_dir, tmp_path, monkeypatch, capsys):
    answer = _evil_answer(tmp_path, monkeypatch)
    assert main(["ask", "--cwd", str(tmp_path), "hi"]) == 0
    assert capsys.readouterr().out == answer + "\n"


def test_tool_names_from_a_server_cannot_repaint_the_terminal(bin_dir, monkeypatch, capsys):
    from mcp_types import Tool

    _add_fake_server()

    async def evil_tools(conn, secret, errlog):
        return [Tool(name=EVIL, inputSchema={"type": "object"})]

    monkeypatch.setattr(commands, "_list_tools", evil_tools)
    assert main(["connect", "test", "gh"]) == 0
    assert main(["connect", "scope", "gh"]) == 0
    out = capsys.readouterr().out
    _assert_no_controls(out)
    assert out.count("a\\x1b]0;owned\\x07") == 2


def test_a_run_shown_from_its_journal_cannot_repaint_the_terminal(echo, tmp_path, capsys):
    from metabot.paths import run_path

    run_id = _one_run(tmp_path, capsys)
    path = run_path(run_id) / "journal.json"
    journal = json.loads(path.read_text(encoding="utf-8"))
    journal["meta"].update(engine=EVIL, model=EVIL, status=EVIL)
    journal["verdicts"] = [{"check": EVIL, "result": EVIL, "detail": EVIL}]
    path.write_text(json.dumps(journal), encoding="utf-8")
    assert main(["receipts", "show", run_id]) == 0
    _assert_no_controls(capsys.readouterr().out)


# ── no command / chat ───────────────────────────────────────────────────────


def test_no_command_without_a_terminal_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main([]) == 0
    assert "usage: metabot" in capsys.readouterr().out


def test_doctor_still_runs(bin_dir, capsys):
    assert main(["init"]) == 0
    assert main(["doctor"]) == 0
    assert "engine   claude  on PATH" in capsys.readouterr().out


class Script:
    """Feeds the chat loop its lines; raising an exception instance stands in for a key."""

    def __init__(self, *lines):
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _result(session="s1", text="answer", receipt=Path("/nowhere.json"), declined=0, failed=0):
    return SimpleNamespace(
        final_text=text,
        session_id=session,
        receipt=receipt,
        steps=0,
        ok=True,
        run_id="r",
        declined=declined,
        failed=failed,
    )


def _loop(lines, run, **kw):
    out: list[str] = []
    trace = Trace(out=io.StringIO())
    script = lines if isinstance(lines, Script) else Script(*lines)
    code = repl(script, out.append, engine="claude", cwd=Path("/work"), trace=trace, run=run, **kw)
    return code, out, trace.out.getvalue()


def test_chat_says_after_an_answer_when_calls_did_not_go_as_asked():
    results = iter([_result(declined=1, failed=2), _result()])
    code, out, traced = _loop(["first", "second"], lambda **kw: next(results))
    assert code == 0 and out.count("answer") == 2
    said = f"[metabot] 1 call was declined and 2 calls did not succeed. {CAVEAT}"
    assert traced.splitlines().count(said) == 1  # after the first answer, not the second


def test_repl_resumes_the_conversation_until_new():
    calls = []

    def run(**kw):
        calls.append(kw)
        return _result()

    code, out, _ = _loop(["first", "second", "/new", "third"], run)
    assert code == 0
    assert [c["resume"] for c in calls] == [None, "s1", None]
    assert [c["prompt"] for c in calls] == ["first", "second", "third"]
    assert out.count("answer") == 3


def test_repl_engine_switch_drops_model_and_session():
    calls = []

    def run(**kw):
        calls.append(kw)
        return _result()

    script = Script("one", "/engine bogus", "/engine codex", "two", "/exit", "never")
    code, out, _ = _loop(script, run, options={"model": "small", "allow_write": True})
    assert code == 0
    assert calls[0]["engine_name"] == "claude" and calls[0]["model"] == "small"
    assert calls[1]["engine_name"] == "codex" and "model" not in calls[1]
    assert calls[1]["resume"] is None and calls[1]["allow_write"] is True
    assert any("unknown engine 'bogus'" in line for line in out)
    assert script.prompts[-1] == "codex> "
    assert len(calls) == 2  # nothing after /exit


def test_repl_ctrl_c_cancels_the_run_not_the_loop():
    sealed = _result(session="s9")
    calls = []

    def run(**kw):
        calls.append(kw)
        if len(calls) == 1:
            try:
                raise RunCancelled(sealed)
            except asyncio.CancelledError:
                raise KeyboardInterrupt from None
        return _result()

    code, out, _ = _loop(["slow one", KeyboardInterrupt(), "next"], run)
    assert code == 0
    assert "cancelled; receipt /nowhere.json" in out
    assert calls[1]["resume"] == "s9"  # the cancelled turn's session carries on


def test_repl_answers_cannot_repaint_the_terminal():
    code, out, _ = _loop(["hi"], lambda **kw: _result(text="one\n\ttwo " + EVIL))
    assert code == 0
    _assert_no_controls("\n".join(out))
    assert any("one\n\ttwo a\\x1b]0;owned\\x07" in line for line in out)


def test_repl_receipt_and_help():
    code, out, _ = _loop(["/receipt", "/help", "/what"], lambda **kw: _result())
    assert "no run yet" in out
    assert any("/engine NAME" in line for line in out)
    assert any("unknown command /what" in line for line in out)


def test_repl_runs_a_real_turn_through_the_engine(echo, tmp_path):
    out: list[str] = []
    trace = Trace(out=io.StringIO())
    assert repl(Script("say it"), out.append, engine="claude", cwd=tmp_path, trace=trace) == 0
    assert "metabot-probe" in out
    assert "(valid, 1 steps)" in trace.out.getvalue()


def test_trace_shows_notices_and_errors_and_quiet_keeps_only_errors():
    from metabot.events import ErrorEvent, Notice

    loud, quiet = Trace(out=io.StringIO()), Trace(out=io.StringIO(), quiet=True)
    for trace in (loud, quiet):
        trace(Notice("codex: Reconnecting... 1/5"))
        trace(ErrorEvent("codex: turn failed: \x1b[2Jboom"))
    assert loud.out.getvalue().splitlines() == [
        "[metabot] codex: Reconnecting... 1/5",
        "[metabot] codex: turn failed: \\x1b[2Jboom",
    ]
    assert quiet.out.getvalue().splitlines() == ["[metabot] codex: turn failed: \\x1b[2Jboom"]


def test_trace_shows_a_call_that_returned_an_error_on_one_readable_line():
    from metabot.events import ToolCall, ToolResult

    trace = Trace(out=io.StringIO())
    trace(ToolCall("c1", "Bash", {"command": "ls -a .cursor/rules"}))
    trace(ToolResult("c1", "Exit code 2\nls: no such folder\n\n  \x1b[2Jboom\r\n", False))
    trace(ToolCall("c2", "shellToolCall", {"command": "python3 -m unittest -q"}))
    trace(ToolResult("c2", {"failure": {"exitCode": 1}}, False))
    trace(ToolResult("c3", "fine", True))
    assert trace.out.getvalue().splitlines() == [
        "  - Bash  ls -a .cursor/rules",
        "    Bash returned an error: Exit code 2 | ls: no such folder | \\x1b[2Jboom",
        "  - shellToolCall  python3 -m unittest -q",
        '    shellToolCall returned an error: {"failure": {"exitCode": 1}}',
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["ask", "-e", "openai", "--model=--dangerously-skip-permissions", "hi"],
        ["ask", "-e", "openai", "-m", "bad model!", "hi"],
        ["chat", "-e", "openai", "--model=-x"],
    ],
)
def test_a_model_that_is_not_a_model_name_never_reaches_an_engine(argv, capsys, home):
    # a model goes on the engine's argv after its flag, so it is never a flag itself
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    assert "is not a model name" in capsys.readouterr().err
    assert not (home / "runs").exists()  # no run began


def test_a_model_with_a_quantization_tag_is_a_model_name():
    from metabot.cli import _model_name

    assert _model_name("qwen2.5-coder:7b-instruct-q4_K_M") == "qwen2.5-coder:7b-instruct-q4_K_M"
