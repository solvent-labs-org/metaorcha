"""Runs at the same time: each gets its own id, its own valid receipt and one index row."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from emerge.record import key_did, load_signing_key, write_key_file

from conftest import FAKE_CLI, FIXTURES
from metabot import receipts, runner
from metabot.approvals import DenyAll
from metabot.events import Final, Started, ToolCall, ToolResult
from metabot.gate import Policy
from metabot.paths import claim_run_dir
from metabot.receipts import ensure_key, journal_matches, list_index, verify_file
from metabot.runner import run_job


class Slow:
    """An in-process engine that reads, waits, then answers; it notes when it ran."""

    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self, name: str, delay: float, spans: list[tuple[float, float]]) -> None:
        self.name = name
        self.delay = delay
        self.spans = spans

    async def run(self, job, *, board=None, approve):
        began = time.monotonic()
        yield Started(f"s-{self.name}", job.model)
        yield ToolCall("c0", "Read", {"path": "."})
        await asyncio.sleep(self.delay)
        yield ToolResult("c0", f"{self.name} read", True)
        self.spans.append((began, time.monotonic()))
        yield Final(f"{self.name} done", f"s-{self.name}")


async def test_three_runs_at_once_give_three_valid_receipts(tmp_path, home):
    spans: list[tuple[float, float]] = []
    results = await asyncio.gather(
        *(
            run_job(
                engine=Slow(f"slow{n}", delay, spans),
                prompt=f"task {n}",
                cwd=tmp_path,
                policy=Policy(),
                approver=DenyAll(),
            )
            for n, delay in enumerate((0.3, 0.1, 0.2))
        )
    )
    assert max(b for b, _ in spans) < min(e for _, e in spans)  # all three ran at once
    ids = [r.run_id for r in results]
    assert len(set(ids)) == 3 and all(r.ok for r in results)
    lines = (home / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["run_id"] for line in lines) == sorted(ids)
    assert sorted(row["run_id"] for row in list_index()) == sorted(ids)
    for result in results:
        assert verify_file(result.receipt).valid
        assert journal_matches(result.run_id)


def test_three_processes_at_once_give_three_receipts_and_one_signer(tmp_path, home):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_FIXTURE": str(FIXTURES / "claude-echo.jsonl"),
    }
    env.pop("CLAUDECODE", None)
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    assert not key_path.exists()  # a new machine: the runs race to make its key
    argv = [sys.executable, "-m", "metabot", "ask", "-e", "claude", "-q", "--cwd", str(tmp_path)]
    procs = [
        subprocess.Popen(
            [*argv, f"probe {n}"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # no terminal to ask on
        )
        for n in range(3)
    ]
    try:
        said = [p.communicate(timeout=120) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    assert [p.returncode for p in procs] == [0, 0, 0], said
    rows = list_index()
    assert len(rows) == 3 and len({row["run_id"] for row in rows}) == 3
    held = key_did(load_signing_key())
    for row in rows:
        verdict = verify_file(row["receipt"])
        assert verdict.valid and verdict.signer_did == held
        assert journal_matches(row["run_id"])


# ── the races, one at a time ────────────────────────────────────────────────


def test_a_key_another_run_made_first_is_the_one_used(tmp_path, monkeypatch):
    real_link = os.link
    theirs: list[str] = []

    def another_run_wins(src, dst, *a, **kw):
        if not theirs:  # the other run's key lands first; its own link is the real one
            theirs.append("")
            _, key = write_key_file()
            theirs[0] = key_did(key)
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", another_run_wins)
    _, did = ensure_key()
    assert theirs and did == theirs[0] == key_did(load_signing_key())
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    assert sorted(p.name for p in key_path.parent.iterdir()) == [key_path.name]


def test_a_new_key_is_whole_before_it_is_in_place(monkeypatch):
    real_link = os.link
    seen: list[str] = []

    def look(src, dst, *a, **kw):
        seen.append(key_did(load_signing_key(src)))  # what a reader would find
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", look)
    _, did = ensure_key()
    assert seen == [did]
    _, again = ensure_key()
    assert again == did and seen == [did]  # a key in place is only read


def _no_hard_links(monkeypatch) -> None:
    def refused(src, dst, *a, **kw):  # as on a FAT or SMB home
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", refused)


def test_a_key_is_made_where_the_filesystem_keeps_no_hard_links(monkeypatch):
    _no_hard_links(monkeypatch)
    _, did = ensure_key()
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    assert did == key_did(load_signing_key()) and ensure_key()[1] == did
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert sorted(p.name for p in key_path.parent.iterdir()) == [key_path.name]


def test_a_key_another_run_is_still_writing_is_waited_for(monkeypatch):
    _no_hard_links(monkeypatch)
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.touch(mode=0o600)  # the other run has made the file and is writing it
    theirs: list[str] = []

    def finish() -> None:
        time.sleep(0.2)
        _, key = write_key_file(key_path, force=True)
        theirs.append(key_did(key))

    writer = threading.Thread(target=finish)
    writer.start()
    try:
        _, did = ensure_key()
    finally:
        writer.join()
    assert theirs and did == theirs[0]


def test_with_no_hard_links_a_key_another_run_made_first_is_used(monkeypatch):
    _no_hard_links(monkeypatch)
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    real_open = os.open
    theirs: list[str] = []

    def another_run_first(path, flags, *a, **kw):
        if Path(path) == key_path and flags & os.O_EXCL and not theirs:
            theirs.append("")  # the other run's own exclusive create is the real one
            _, key = write_key_file(key_path)
            theirs[0] = key_did(key)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", another_run_first)
    _, did = ensure_key()
    assert theirs and did == theirs[0]


def test_a_key_that_cannot_be_written_whole_leaves_no_key_file(monkeypatch):
    _no_hard_links(monkeypatch)
    real_fsync = os.fsync
    calls: list[int] = []

    def full(fd):
        calls.append(fd)
        if len(calls) == 2:  # the key file's own, after the private copy's
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", full)
    # named, so the command stops on one line instead of a traceback
    with pytest.raises(receipts.KeyFileError, match="^cannot make the signing key .*No space"):
        ensure_key()
    assert not Path(os.environ["ORCHA_KEY_PATH"]).exists()


def test_a_key_file_that_never_reads_is_still_refused(monkeypatch):
    monkeypatch.setattr(receipts, "KEY_WAIT_S", 0.1)
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("not a key\n", encoding="ascii")
    with pytest.raises(ValueError):
        ensure_key()


def test_a_new_key_is_private():
    ensure_key()
    key_path = Path(os.environ["ORCHA_KEY_PATH"])
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_a_run_id_another_run_took_is_drawn_again(tmp_path, monkeypatch):
    taken = "mb-20261012T000000Z-aaaaaa"
    claim_run_dir(taken)
    drawn = iter([taken, taken, "mb-20261012T000000Z-bbbbbb"])
    monkeypatch.setattr(runner, "new_run_id", lambda: next(drawn))
    result = asyncio.run(
        run_job(
            engine=Slow("slow", 0, []),
            prompt="task",
            cwd=tmp_path,
            policy=Policy(),
            approver=DenyAll(),
        )
    )
    assert result.run_id == "mb-20261012T000000Z-bbbbbb" and verify_file(result.receipt).valid


def test_a_run_id_given_and_taken_is_refused(tmp_path):
    taken = "mb-20261012T000000Z-cccccc"
    claim_run_dir(taken)
    with pytest.raises(ValueError, match="run id in use"):
        asyncio.run(
            run_job(
                engine=Slow("slow", 0, []),
                prompt="task",
                cwd=tmp_path,
                policy=Policy(),
                approver=DenyAll(),
                run_id=taken,
            )
        )


def test_a_new_key_folder_is_private_and_an_existing_one_is_left_alone(tmp_path, monkeypatch):
    # where the umask lets the group write, a key folder made with it would let
    # a group member put another key in place; MetaBot makes the folder 0700
    old = os.umask(0o002)
    try:
        fresh = tmp_path / "fresh-home" / ".orcha" / "key"
        monkeypatch.setenv("ORCHA_KEY_PATH", str(fresh))
        ensure_key()
        assert stat.S_IMODE(fresh.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o600

        shared = tmp_path / "chosen"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        monkeypatch.setenv("ORCHA_KEY_PATH", str(shared / "key"))
        ensure_key()
        assert stat.S_IMODE(shared.stat().st_mode) == 0o755  # a folder the person chose is theirs
    finally:
        os.umask(old)
