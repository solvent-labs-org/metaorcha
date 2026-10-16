from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time

import pytest

from fake_engines import FakeEngine
from metabot.engines import available, get_engine
from metabot.engines.base import DROPPED_ENV, EngineJob, run_cli
from metabot.events import ErrorEvent, Final, TextEvent

PROMPT = 'a "b" $(rm -rf /) \n -x'


def _fixture(tmp_path, lines):
    path = tmp_path / "stream.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    return path


async def _collect(job):
    async with contextlib.aclosing(run_cli(FakeEngine(), job)) as stream:
        return [event async for event in stream]


async def test_prompt_is_one_argv_element(tmp_path, monkeypatch):
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    await _collect(EngineJob(prompt=PROMPT, cwd=tmp_path))
    argv = json.loads(out.read_text())["argv"]
    assert argv[-2:] == ["--prompt", PROMPT]


async def test_stdin_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_READ_STDIN", "1")
    t0 = time.monotonic()
    await asyncio.wait_for(_collect(EngineJob(prompt="x", cwd=tmp_path)), timeout=5)
    assert time.monotonic() - t0 < 5


async def test_nonzero_exit_yields_error_with_stderr_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.setenv("FAKE_STDERR", "x" * 5000 + "not logged in")
    events = await _collect(EngineJob(prompt="x", cwd=tmp_path))
    [err] = [e for e in events if isinstance(e, ErrorEvent)]
    assert err.message.startswith("fake exited 3: ")
    assert err.message.endswith("not logged in")
    assert len(err.message) <= len("fake exited 3: ") + 2000


async def test_session_vars_dropped(tmp_path, monkeypatch):
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    for var in DROPPED_ENV:
        monkeypatch.setenv(var, "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/.claude-x")
    await _collect(EngineJob(prompt="x", cwd=tmp_path))
    seen = json.loads(out.read_text())
    assert not set(seen["env_keys"]) & DROPPED_ENV
    assert "CLAUDECODE" in DROPPED_ENV
    assert seen["claude_config_dir"] == "/somewhere/.claude-x"


async def test_malformed_line_is_text(tmp_path, monkeypatch):
    fixture = _fixture(
        tmp_path,
        [
            "not json at all",
            "[1, 2]",
            '{"type": "boom"}',
            '{"type": "text", "text": "hello"}',
            '{"type": "final", "text": "done", "session_id": "s1"}',
        ],
    )
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    events = await _collect(EngineJob(prompt="x", cwd=tmp_path))
    assert events == [
        TextEvent("not json at all"),
        TextEvent("[1, 2]"),
        TextEvent('{"type": "boom"}'),
        TextEvent("hello"),
        Final("done", "s1"),
    ]


async def test_cwd_is_the_jobs(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_ARGV_OUT", "argv.json")  # relative: lands in the child's cwd
    await _collect(EngineJob(prompt="x", cwd=work))
    assert (work / "argv.json").exists()


async def test_missing_binary_is_an_error_not_a_crash(tmp_path):
    class Missing(FakeEngine):
        def argv(self, job):
            return [str(tmp_path / "no-such-cli"), "-p", job.prompt]

    async with contextlib.aclosing(run_cli(Missing(), EngineJob("x", tmp_path))) as stream:
        events = [e async for e in stream]
    [err] = events
    assert isinstance(err, ErrorEvent)
    assert "not found" in err.message


async def test_closing_early_kills_the_engine(tmp_path, monkeypatch):
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(out))
    monkeypatch.setenv("FAKE_FIXTURE", str(_fixture(tmp_path, ['{"type":"text","text":"hi"}'])))
    monkeypatch.setenv("FAKE_SLEEP", "60")
    t0 = time.monotonic()
    async with contextlib.aclosing(run_cli(FakeEngine(), EngineJob("x", tmp_path))) as stream:
        async for event in stream:
            assert event == TextEvent("hi")
            break
    pid = json.loads(out.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert time.monotonic() - t0 < 10


async def test_overlong_line_is_dropped_not_fatal(tmp_path, monkeypatch):
    from metabot.engines import base

    monkeypatch.setattr(base, "MAX_LINE", 1000)
    fixture = _fixture(
        tmp_path,
        ['{"type":"text","text":"' + "y" * 5000 + '"}', '{"type":"text","text":"after"}'],
    )
    monkeypatch.setenv("FAKE_FIXTURE", str(fixture))
    events = await _collect(EngineJob(prompt="x", cwd=tmp_path))
    assert events[-1] == TextEvent("after")
    assert any("dropped" in e.text for e in events if isinstance(e, TextEvent))


def test_get_engine_rejects_unknown():
    with pytest.raises(ValueError, match="unknown engine"):
        get_engine("nope")


def test_available_maps_names_to_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    found = available()
    assert set(found) >= {"claude", "codex", "cursor"}
    assert found["claude"] is None


async def test_a_leftover_holding_stdout_does_not_hang_the_run(tmp_path, monkeypatch):
    import signal

    from metabot.engines import base

    holder = tmp_path / "holder.pid"
    monkeypatch.setenv("FAKE_HOLDER_OUT", str(holder))
    monkeypatch.setenv("FAKE_FIXTURE", str(_fixture(tmp_path, ['{"type":"final","text":"ok"}'])))
    t0 = time.monotonic()
    try:
        events = await asyncio.wait_for(_collect(EngineJob("x", tmp_path)), timeout=15)
    finally:
        if holder.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(holder.read_text()), signal.SIGKILL)
    assert events == [Final("ok", None)]
    assert time.monotonic() - t0 < base.DRAIN_GRACE + 3


async def test_missing_cwd_is_named(tmp_path):
    job = EngineJob("x", tmp_path / "gone")
    async with contextlib.aclosing(run_cli(FakeEngine(), job)) as stream:
        [err] = [e async for e in stream]
    assert isinstance(err, ErrorEvent)
    assert "working directory" in err.message


async def test_prepare_failure_is_an_error_not_a_crash(tmp_path):
    class Unprepared(FakeEngine):
        def prepare(self, job):
            raise OSError("disk full")

    async with contextlib.aclosing(run_cli(Unprepared(), EngineJob("x", tmp_path))) as stream:
        [err] = [e async for e in stream]
    assert isinstance(err, ErrorEvent)
    assert "disk full" in err.message
