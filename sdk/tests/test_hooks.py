"""The Claude Code hook adapter, driven by payloads captured on 2.1.278.

The fixtures below are the field shapes observed on 2026-09-21 (ids, paths and
outputs replaced): a failing ``Bash`` call fires ``PostToolUseFailure`` with
``error: "Exit code N"`` and no ``tool_response``; a succeeding one fires
``PostToolUse`` with a ``tool_response`` object; ``Stop`` ends the turn.
"""

from __future__ import annotations

import io
import json

import pytest
from emerge.cli import main
from emerge.hooks import claude_code_settings, claude_code_step, run_claude_code_hook
from emerge.journal import journal_path, load_journal, receipt_path
from emerge.record import ALLOW_EPHEMERAL_KEY_ENV, KEY_PATH_ENV, PRIVATE_KEY_ENV
from emerge.run_attestation import verify_run_attestation

SESSION = "4ed9bcbc-e726-407d-b67d-1dce2b6faade"


def _pre(command: str, tool_use_id: str, cwd: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "cwd": cwd,
        "permission_mode": "default",
        "transcript_path": "/home/someone/.claude/projects/x/transcript.jsonl",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "spike"},
        "tool_use_id": tool_use_id,
    }


def _failure(command: str, code: int, tool_use_id: str, cwd: str) -> dict:
    return {
        **_pre(command, tool_use_id, cwd),
        "hook_event_name": "PostToolUseFailure",
        "duration_ms": 51,
        "error": f"Exit code {code}",
        "is_interrupt": False,
    }


def _success(command: str, stdout: str, tool_use_id: str, cwd: str) -> dict:
    return {
        **_pre(command, tool_use_id, cwd),
        "hook_event_name": "PostToolUse",
        "duration_ms": 28,
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
    }


def _stop(cwd: str) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": SESSION,
        "cwd": cwd,
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "done",
        "transcript_path": "/home/someone/.claude/projects/x/transcript.jsonl",
    }


@pytest.fixture
def key_file(monkeypatch, tmp_path):
    """A minted key at ORCHA_KEY_PATH; no env seed, no ephemeral opt-in."""
    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)
    path = tmp_path / "keys" / "key"
    monkeypatch.setenv(KEY_PATH_ENV, str(path))
    assert main(["record", "keygen"]) == 0
    return path


# ── Payload → step ───────────────────────────────────────────────────────────


def test_failure_event_becomes_a_failed_step_with_the_parsed_exit_code(tmp_path):
    step = claude_code_step(_failure("exit 3", 3, "toolu_01A", str(tmp_path)))
    assert step == {
        "call_id": "toolu_01A",
        "tool": "claude-code/Bash",
        "args": {"command": "exit 3", "description": "spike"},
        "output": {"error": "Exit code 3", "exit_code": 3},
        "success": False,
        "latency_ms": 51,
    }


def test_success_event_becomes_a_passing_step_with_exit_code_zero(tmp_path):
    step = claude_code_step(_success("echo ok", "ok", "toolu_01B", str(tmp_path)))
    assert step["success"] is True
    assert step["output"]["exit_code"] == 0
    assert step["output"]["stdout"] == "ok"
    assert step["latency_ms"] == 28


def test_non_bash_tools_are_recorded_without_an_exit_code(tmp_path):
    payload = _success("", "", "toolu_01C", str(tmp_path))
    payload["tool_name"] = "Read"
    payload["tool_input"] = {"file_path": "/x"}
    payload["tool_response"] = {"type": "text", "file": {"content": "..."}}
    step = claude_code_step(payload)
    assert step["tool"] == "claude-code/Read"
    assert "exit_code" not in step["output"]


def test_failure_without_a_parseable_code_has_no_exit_code(tmp_path):
    payload = _failure("x", 0, "toolu_01D", str(tmp_path))
    payload["error"] = "Command timed out"
    step = claude_code_step(payload)
    assert step["output"] == {"error": "Command timed out"}
    assert step["success"] is False


def test_step_never_copies_transcript_path_or_permission_mode(tmp_path):
    step = claude_code_step(_success("true", "", "toolu_01E", str(tmp_path)))
    assert "transcript_path" not in json.dumps(step)
    assert "permission_mode" not in json.dumps(step)


def test_other_events_map_to_nothing(tmp_path):
    assert claude_code_step(_pre("true", "toolu_01F", str(tmp_path))) is None
    assert claude_code_step(_stop(str(tmp_path))) is None


# ── The session walk: two calls, then Stop seals ─────────────────────────────


def test_session_journals_then_seals_a_receipt_that_verifies(tmp_path, key_file):
    cwd = str(tmp_path)
    err = io.StringIO()
    criteria = {"exit_zero": True}

    assert run_claude_code_hook(_pre("exit 3", "t1", cwd), criteria=criteria) == 0
    assert not journal_path(tmp_path, SESSION).exists()  # PreToolUse records nothing

    assert (
        run_claude_code_hook(_failure("exit 3", 3, "t1", cwd), criteria=criteria) == 0
    )
    assert (
        run_claude_code_hook(_success("echo ok", "ok", "t2", cwd), criteria=criteria)
        == 0
    )
    journal = load_journal(journal_path(tmp_path, SESSION))
    assert journal["harness"] == "claude-code"
    assert journal["criteria"] == criteria
    assert [s["call_id"] for s in journal["steps"]] == ["t1", "t2"]

    assert run_claude_code_hook(_stop(cwd), criteria=criteria, err=err) == 0
    receipt = receipt_path(tmp_path, SESSION)
    envelope = json.loads(receipt.read_text())
    assert envelope["run_id"] == SESSION
    assert len(envelope["steps"]) == 2
    assert envelope["verdicts"] == [
        {
            "check": "declared_acceptance",
            "result": "fail",
            "detail": "exit_zero: nonzero exit: 3",
        }
    ]
    assert verify_run_attestation(envelope).valid
    assert f"verify with: orcha verify {receipt}" in err.getvalue()
    assert "Exit code 3" not in receipt.read_text()


def test_a_passing_session_gets_a_passing_verdict(tmp_path, key_file):
    cwd = str(tmp_path)
    run_claude_code_hook(_success("true", "", "t1", cwd), criteria={"exit_zero": True})
    assert run_claude_code_hook(_stop(cwd), criteria={"exit_zero": True}) == 0
    envelope = json.loads(receipt_path(tmp_path, SESSION).read_text())
    assert envelope["verdicts"][0]["result"] == "pass"


def test_stop_with_no_journal_is_a_quiet_zero(tmp_path, key_file):
    err = io.StringIO()
    assert run_claude_code_hook(_stop(str(tmp_path)), err=err) == 0
    assert err.getvalue() == ""
    assert not receipt_path(tmp_path, SESSION).exists()


def test_stop_reseals_the_whole_session_each_turn(tmp_path, key_file):
    cwd = str(tmp_path)
    run_claude_code_hook(_success("true", "", "t1", cwd))
    run_claude_code_hook(_stop(cwd))
    first = json.loads(receipt_path(tmp_path, SESSION).read_text())
    run_claude_code_hook(_success("true", "", "t2", cwd))
    run_claude_code_hook(_stop(cwd))
    second = json.loads(receipt_path(tmp_path, SESSION).read_text())
    assert len(first["steps"]) == 1
    assert len(second["steps"]) == 2
    assert second["started_at"] == first["started_at"]


def test_a_redelivered_event_is_not_a_second_step(tmp_path, key_file):
    cwd = str(tmp_path)
    run_claude_code_hook(_success("true", "", "t1", cwd))
    run_claude_code_hook(_success("true", "", "t1", cwd))
    assert len(load_journal(journal_path(tmp_path, SESSION))["steps"]) == 1


# ── Never break the session ──────────────────────────────────────────────────


def test_missing_key_on_stop_is_a_non_blocking_one_and_keeps_the_journal(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)
    monkeypatch.setenv(KEY_PATH_ENV, str(tmp_path / "absent"))
    cwd = str(tmp_path)
    err = io.StringIO()
    assert run_claude_code_hook(_success("true", "", "t1", cwd)) == 0
    assert run_claude_code_hook(_stop(cwd), err=err) == 1
    assert "not sealed" in err.getvalue()
    assert "orcha record keygen" in err.getvalue()
    assert journal_path(tmp_path, SESSION).exists()
    assert not receipt_path(tmp_path, SESSION).exists()


def test_payload_without_session_id_is_a_non_blocking_one(tmp_path):
    err = io.StringIO()
    payload = _success("true", "", "t1", str(tmp_path))
    del payload["session_id"]
    assert run_claude_code_hook(payload, err=err) == 1
    assert "no session_id" in err.getvalue()


def test_unsafe_session_id_never_escapes_dot_orcha(tmp_path):
    err = io.StringIO()
    payload = _success("true", "", "t1", str(tmp_path))
    payload["session_id"] = "../../escape"
    assert run_claude_code_hook(payload, err=err) == 1
    assert not (tmp_path / ".orcha").exists()


def test_root_overrides_the_payload_cwd(tmp_path, key_file):
    elsewhere = tmp_path / "elsewhere"
    run_claude_code_hook(
        _success("true", "", "t1", "/nonexistent"), root=str(elsewhere)
    )
    assert journal_path(elsewhere, SESSION).exists()


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_hook_reads_stdin_and_exits_zero(tmp_path, key_file, monkeypatch, capsys):
    cwd = str(tmp_path)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_success("true", "", "t1", cwd)))
    )
    assert main(["record", "hook", "claude-code", "--criteria", "exit_zero"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_stop(cwd))))
    assert main(["record", "hook", "claude-code", "--criteria", "exit_zero"]) == 0
    out, err = capsys.readouterr()
    assert out == ""  # a hook's stdout is the harness's; we write nothing to it
    assert "sealed run_id=" in err
    envelope = json.loads(receipt_path(tmp_path, SESSION).read_text())
    assert envelope["policy_version"].startswith("local-session/1.0+criteria:")


def test_cli_hook_rejects_bad_stdin_without_blocking(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["record", "hook", "claude-code"]) == 1
    assert "malformed hook payload" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("[1]"))
    assert main(["record", "hook", "claude-code"]) == 1


def test_cli_hook_rejects_an_unknown_criterion_before_reading_stdin(capsys):
    assert main(["record", "hook", "claude-code", "--criteria", "nope"]) == 1
    assert "unsupported criterion 'nope'" in capsys.readouterr().err


def test_cli_print_settings_emits_the_three_event_block(capsys):
    assert (
        main(
            [
                "record",
                "hook",
                "claude-code",
                "--print-settings",
                "--criteria",
                "exit_zero",
                "--key",
                "/k ey",
            ]
        )
        == 0
    )
    block = json.loads(capsys.readouterr().out)
    assert set(block["hooks"]) == {"PostToolUse", "PostToolUseFailure", "Stop"}
    command = block["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert command == "orcha record hook claude-code --key '/k ey' --criteria exit_zero"
    assert claude_code_settings("x")["hooks"]["PostToolUse"][0]["hooks"][0]["type"] == (
        "command"
    )
