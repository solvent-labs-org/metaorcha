from __future__ import annotations

import io
import os
import subprocess
import time

from metabot.serverlog import CHUNK, JOIN_S, RedactedLog

TOKEN = "tok-abcdef-0123456789"
SECRETS = {"GH_TOKEN": TOKEN}
MARK = "[REDACTED:GH_TOKEN]"


def _raw(log: RedactedLog, data: bytes) -> None:
    """Write as a server does: straight to the file descriptor, no Python buffering."""
    os.write(log.writer.fileno(), data)


def test_a_token_split_across_writes_is_still_replaced():
    out = io.StringIO()
    log = RedactedLog(out, SECRETS)
    _raw(log, b"auth " + TOKEN[:7].encode())
    time.sleep(0.2)  # the copy sees the first half alone
    _raw(log, TOKEN[7:].encode() + b" ok\n")
    log.close()
    assert out.getvalue() == f"auth {MARK} ok\n"


def test_a_line_that_never_ends_is_written_without_the_token():
    out = io.StringIO()
    log = RedactedLog(out, SECRETS)
    body = b"x" * (3 * CHUNK) + TOKEN.encode() + b"y" * (2 * CHUNK)
    for i in range(0, len(body), 4096):  # many small writes, never a newline
        _raw(log, body[i : i + 4096])
    log.close()
    assert out.getvalue() == "x" * (3 * CHUNK) + MARK + "y" * (2 * CHUNK)


def test_without_secrets_the_text_is_copied_exactly():
    out = io.StringIO()
    log = RedactedLog(out, {})
    _raw(log, b"line one\npartial")
    log.close()
    assert out.getvalue() == "line one\npartial"


def test_bad_bytes_and_split_characters_are_decoded_not_fatal():
    out = io.StringIO()
    log = RedactedLog(out, SECRETS)
    snowman = "\u2603".encode()
    _raw(log, b"bad \xff\xfe then " + snowman[:1])
    time.sleep(0.2)
    _raw(log, snowman[1:] + b"\n")
    log.close()
    assert out.getvalue() == "bad \ufffd\ufffd then \u2603\n"


def test_notes_go_through_the_same_redaction():
    out = io.StringIO()
    log = RedactedLog(out, SECRETS)
    log.note(f"metabot: connection gh skipped: bad token {TOKEN}")
    log.close()
    assert out.getvalue() == f"metabot: connection gh skipped: bad token {MARK}\n"


def test_close_does_not_wait_on_a_process_that_keeps_the_pipe():
    out = io.StringIO()
    log = RedactedLog(out, SECRETS)
    holder = subprocess.Popen(["sleep", "30"], stderr=log.writer)
    try:
        started = time.monotonic()
        log.close()
        assert time.monotonic() - started < JOIN_S + 1.0
    finally:
        holder.kill()
        holder.wait()
    log._thread.join(5)  # the copy ends once the last writer is gone
    assert not log._thread.is_alive()
