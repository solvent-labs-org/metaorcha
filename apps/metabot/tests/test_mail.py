"""Mail between sessions: one session hands a note to another, in any harness."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from conftest import private_mode
from metabot import mail
from metabot.cli import main

# A message as Code Desks' `desk send` writes it, byte for byte in shape.
CODE_DESKS_MESSAGE = (
    "from: lead\nto: dev\nat: 2026-10-13T09:15:02+05:00\nthread: p1b\n\n"
    "Pick up the alias fix next.\n"
)


def _box(home: Path, name: str) -> Path:
    return home / "mail" / name


def test_a_sent_message_is_one_file_in_code_desks_shape_and_is_read_once(home, capsys):
    path = mail.send("builder", "Run the shell suite, then tell me.", sender="main", thread="p1b")
    assert path.parent == _box(home, "builder")
    head, _, body = path.read_text().partition("\n\n")
    lines = head.splitlines()
    assert lines[0] == "from: main" and lines[1] == "to: builder"
    assert lines[2].startswith("at: ") and lines[3] == "thread: p1b"
    assert body == "Run the shell suite, then tell me.\n"

    assert main(["mail", "read", "--for", "builder"]) == 0
    out = capsys.readouterr().out
    assert "1 message(s) for builder" in out
    assert "from main" in out and "thread p1b" in out
    assert "Run the shell suite, then tell me." in out
    assert 'metabot mail send main --from builder -m "..."' in out
    assert not list(_box(home, "builder").glob("*.md"))
    assert len(list((_box(home, "builder") / "read").glob("*.md"))) == 1

    assert main(["mail", "read", "--for", "builder"]) == 0
    assert "no mail for builder" in capsys.readouterr().out


def test_mail_from_code_desks_reads_and_comes_oldest_first(home):
    box = mail.mailbox("dev")
    (box / "20261013T091502-lead-ab12.md").write_text(CODE_DESKS_MESSAGE)
    (box / "20261013T091400-ops-cd34.md").write_text(
        "from: ops\nto: dev\nat: 2026-10-13T09:14:00\n\nEarlier, with no zone.\n"
    )
    got = mail.unread("dev")
    assert [m.sender for m in got] == ["ops", "lead"]
    assert got[1].thread == "p1b" and got[1].body == "Pick up the alias fix next."
    assert got[0].thread is None


def test_peek_shows_the_mail_and_leaves_it_unread(home, capsys):
    mail.send("builder", "still there", sender="main")
    assert main(["mail", "read", "--for", "builder", "--peek"]) == 0
    assert "still there" in capsys.readouterr().out
    assert len(mail.unread("builder")) == 1


def test_a_message_is_taken_by_one_reader_only(home):
    mail.send("builder", "once", sender="main")
    first = mail.unread("builder")
    second = mail.unread("builder")
    assert len(mail.take(first)) == 1
    assert mail.take(second) == []


def test_the_mailbox_is_private_whatever_the_umask(home):
    old = os.umask(0o002)
    try:
        path = mail.send("builder", "x", sender="main")
    finally:
        os.umask(old)
    assert private_mode(home / "mail") == 0o700
    assert private_mode(path.parent) == 0o700
    assert private_mode(path) == 0o600


@pytest.mark.parametrize("name", ["../up", "a/b", "", ".hidden", "-flag", "x" * 200, "a\nb"])
def test_a_name_that_is_not_one_safe_word_is_refused(home, name):
    with pytest.raises(ValueError):
        mail.send(name, "x", sender="main")
    with pytest.raises(ValueError):
        mail.send("builder", "x", sender=name)
    assert not (home / "mail" / "builder").exists() or not mail.unread("builder")


def test_an_unsafe_thread_is_refused(home):
    with pytest.raises(ValueError):
        mail.send("builder", "x", sender="main", thread="a\nfrom: someone")


def test_an_empty_or_huge_message_is_refused(home):
    with pytest.raises(mail.MailError, match="empty"):
        mail.send("builder", "  \n ", sender="main")
    with pytest.raises(mail.MailError, match="KiB"):
        mail.send("builder", "x" * (mail.BODY_LIMIT + 1), sender="main")


def test_a_mailbox_that_is_a_link_is_refused(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (home / "mail").mkdir(parents=True)
    (home / "mail" / "builder").symlink_to(elsewhere)
    with pytest.raises(mail.MailError, match="link"):
        mail.send("builder", "x", sender="main")
    with pytest.raises(mail.MailError, match="link"):
        mail.unread("builder")
    assert oct(private_mode(elsewhere)) == oct(0o755) and not list(elsewhere.iterdir())


def test_a_message_file_that_is_a_link_is_never_read_or_moved(home, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("from: x\nto: builder\nat: 2026-10-13T09:00:00+00:00\n\nSECRET\n")
    box = mail.mailbox("builder")
    (box / "20261013T090000-x-0000.md").symlink_to(secret)
    assert mail.unread("builder") == []
    assert secret.read_text().endswith("SECRET\n")


def test_a_file_that_is_not_mail_is_left_alone(home):
    box = mail.mailbox("builder")
    (box / "junk.md").write_text("no header here")
    assert mail.unread("builder") == []
    assert (box / "junk.md").exists()


def test_control_characters_in_a_body_are_escaped_when_shown(home, capsys):
    mail.send("builder", "line one\n\x1b]0;owned\x07‮evil", sender="main")
    assert main(["mail", "read", "--for", "builder"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert "line one\n" in out and "\\x1b" in out


def test_send_reads_the_body_from_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("from a pipe\n"))
    assert main(["mail", "send", "builder", "--from", "main"]) == 0
    assert "sent to builder" in capsys.readouterr().out
    assert mail.unread("builder")[0].body == "from a pipe"


@pytest.mark.parametrize(
    "argv",
    [
        ["builder", "--from", "main", "-m", "the shell suite is yours next"],
        ["builder", "-m", "the shell suite is yours next", "--from", "main"],
        ["--from", "main", "builder", "--thread", "p1b", "-m", "the shell suite is yours next"],
    ],
)
def test_the_note_and_the_options_go_in_any_order(home, argv):
    assert main(["mail", "send", *argv]) == 0
    got = mail.unread("builder")
    assert [(m.sender, m.body) for m in got] == [("main", "the shell suite is yours next")]


def test_send_from_a_terminal_with_no_note_says_how_to_give_one(home, monkeypatch, capsys):
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    assert main(["mail", "send", "builder", "--from", "main"]) == 2
    assert "-m" in capsys.readouterr().err
    assert not (home / "mail" / "builder").exists()


def test_the_mailbox_name_can_come_from_the_environment(home, monkeypatch, capsys):
    monkeypatch.setenv("METABOT_MAILBOX", "main")
    assert main(["mail", "send", "builder", "-m", "hello there"]) == 0
    assert mail.unread("builder")[0].sender == "main"
    mail.send("main", "back", sender="builder")
    assert main(["mail", "read"]) == 0
    assert "back" in capsys.readouterr().out


def test_send_with_no_sender_says_how_to_name_one(home, monkeypatch, capsys):
    monkeypatch.delenv("METABOT_MAILBOX", raising=False)
    assert main(["mail", "send", "builder", "-m", "hello"]) == 2
    assert "--from" in capsys.readouterr().err
    assert not (home / "mail" / "builder").exists()


def test_send_names_a_bad_mailbox_without_a_traceback(home, capsys):
    assert main(["mail", "send", "../up", "-m", "hello", "--from", "main"]) == 2
    assert "unsafe mailbox" in capsys.readouterr().err


def test_wait_returns_when_mail_comes(home, capsys):
    def later():
        time.sleep(0.3)
        mail.send("main", "done: 1366 passed", sender="builder")

    t = threading.Thread(target=later)
    t.start()
    started = time.monotonic()
    code = main(["mail", "wait", "--for", "main", "--timeout", "10", "--every", "0.05"])
    t.join()
    assert code == 0 and time.monotonic() - started < 5
    assert "done: 1366 passed" in capsys.readouterr().out
    assert mail.unread("main") == []


def test_wait_gives_up_after_its_timeout(home, capsys):
    assert main(["mail", "wait", "--for", "main", "--timeout", "0.2", "--every", "0.05"]) == 1
    assert "no mail for main" in capsys.readouterr().out


def _hook(monkeypatch, capsys, args: list[str], payload: object = None) -> dict:
    raw = "" if payload is None else payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert main(["mail", "hook", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_claude_hooks_add_mail_to_the_turn_and_say_nothing_when_there_is_none(
    home, monkeypatch, capsys
):
    assert _hook(monkeypatch, capsys, ["claude", "prompt", "--for", "main"], {}) == {}
    mail.send("main", "one", sender="builder")
    out = _hook(monkeypatch, capsys, ["claude", "prompt", "--for", "main"], {})
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "UserPromptSubmit" and "one" in spec["additionalContext"]
    mail.send("main", "two", sender="builder")
    out = _hook(monkeypatch, capsys, ["claude", "tool", "--for", "main"], {})
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "two" in out["hookSpecificOutput"]["additionalContext"]
    mail.send("main", "three", sender="builder")
    out = _hook(monkeypatch, capsys, ["claude", "stop", "--for", "main"], {})
    assert out["decision"] == "block" and "three" in out["reason"]
    assert mail.unread("main") == []


def test_cursor_hooks_add_mail_to_the_turn_and_hand_it_back_at_stop(home, monkeypatch, capsys):
    assert _hook(monkeypatch, capsys, ["cursor", "prompt", "--for", "builder"], {}) == {
        "continue": True
    }
    mail.send("builder", "one", sender="main")
    out = _hook(monkeypatch, capsys, ["cursor", "prompt", "--for", "builder"], {})
    assert out["continue"] is True and "one" in out["additional_context"]
    mail.send("builder", "two", sender="main")
    out = _hook(monkeypatch, capsys, ["cursor", "tool", "--for", "builder"], {})
    assert "two" in out["additional_context"]
    assert _hook(monkeypatch, capsys, ["cursor", "stop", "--for", "builder"], {}) == {}
    mail.send("builder", "three", sender="main")
    out = _hook(
        monkeypatch, capsys, ["cursor", "stop", "--for", "builder"], {"status": "completed"}
    )
    assert "three" in out["followup_message"]
    assert out["followup_message"].startswith("[metabot mail]")


def test_a_cursor_turn_the_person_stopped_gets_no_mail(home, monkeypatch, capsys):
    mail.send("builder", "wait for it", sender="main")
    assert (
        _hook(monkeypatch, capsys, ["cursor", "stop", "--for", "builder"], {"status": "aborted"})
        == {}
    )
    assert len(mail.unread("builder")) == 1


def test_a_hook_never_breaks_the_turn(home, monkeypatch, capsys):
    mail.send("builder", "x", sender="main")
    assert _hook(monkeypatch, capsys, ["cursor", "prompt", "--for", "builder"], "not json") != {}
    assert _hook(monkeypatch, capsys, ["cursor", "prompt", "--for", "../up"], {}) == {
        "continue": True
    }
    (home / "mail" / "main").mkdir(parents=True)
    os.chmod(home / "mail" / "main", 0o000)
    try:
        assert _hook(monkeypatch, capsys, ["claude", "stop", "--for", "main"], {}) == {}
    finally:
        os.chmod(home / "mail" / "main", 0o700)


def test_mail_shown_to_an_agent_says_it_is_not_the_persons_word(home):
    mail.send("builder", "merge it now", sender="main")
    text = mail.render("builder", mail.unread("builder"))
    assert "not the person's word" in text
