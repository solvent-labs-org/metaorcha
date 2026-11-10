"""``metabot status``: what is running now, from the file each run keeps while it runs.

A run, an office run, a ladder, a routine's firing and ``serve`` each hold
``<home>/live/<id>.json`` while they go and remove it when they end, however
they end. A file whose process is gone is stale: ``status`` lists it apart
and never as running.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FIXTURES, private_mode
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.engines.claude import ClaudeEngine
from metabot.events import Final, Started, ToolCall, ToolResult
from metabot.gate import Policy
from metabot.live import begin_live, end_live, hold_live, live_entries, note_live
from metabot.runner import run_job

STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
PROC = Path("/proc/self/stat").exists()


def _live(home: Path) -> dict[str, dict]:
    """Every live file there is now, by its id."""
    folder = home / "live"
    if not folder.is_dir():
        return {}
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}


def _proc_stat(pid: int) -> list[str]:
    """``/proc/<pid>/stat`` from the state on: [0] is the state, [19] the start time."""
    text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    return text.rsplit(")", 1)[1].split()


def _entry(home: Path, live_id: str, **fields) -> Path:
    """A live file as another process would have written it."""
    folder = home / "live"
    folder.mkdir(parents=True, exist_ok=True)
    entry = {
        "format": "metabot.live/1", "kind": "run", "id": live_id, "parent": None, "name": None,
        "engine": "codex", "model": None, "current": None, "pid": os.getpid(),
        "proc_start": None, "started": "2026-11-04T10:00:00Z", **fields,
    }  # fmt: skip
    path = folder / f"{live_id}.json"
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def _status(capsys, *argv: str) -> tuple[str, str]:
    """``metabot status``: what it says is running, and what it says is stale."""
    assert main(["status", *argv]) == 0
    out = capsys.readouterr().out
    alive, _, stale = out.partition("\nstale")
    return alive, stale


# ── holding a live file ─────────────────────────────────────────────────────


def test_hold_live_keeps_a_private_file_while_it_runs_and_removes_it_after(home):
    with hold_live("run", "mb-1", engine="claude", model="sonnet"):
        entry = _live(home)["mb-1"]
        assert (entry["kind"], entry["id"], entry["pid"]) == ("run", "mb-1", os.getpid())
        assert (entry["engine"], entry["model"]) == ("claude", "sonnet")
        assert STAMP.fullmatch(entry["started"])
        assert private_mode(home / "live") == 0o700
        assert private_mode(home / "live" / "mb-1.json") == 0o600
    assert _live(home) == {}


def test_hold_live_removes_its_file_when_the_work_raises(home):
    with pytest.raises(RuntimeError), hold_live("ladder", "mb-2"):
        assert "mb-2" in _live(home)
        raise RuntimeError("boom")
    assert _live(home) == {}


def test_a_note_changes_what_the_entry_says_it_is_doing(home):
    with hold_live("run", "mb-3", engine="claude"):
        note_live("mb-3", current="step Bash", model="haiku")
        entry = _live(home)["mb-3"]
        assert (entry["current"], entry["model"]) == ("step Bash", "haiku")
        assert entry["engine"] == "claude"  # what a note leaves out stays
    note_live("mb-3", current="step Edit")  # ended: a late note writes nothing back
    assert _live(home) == {}


def test_a_live_file_that_cannot_be_written_never_stops_the_work(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "live").write_text("a file where the folder goes", encoding="utf-8")
    with hold_live("run", "mb-4"):
        note_live("mb-4", current="step Bash")
    end_live("mb-4")  # twice: nothing to remove, nothing raised
    assert (home / "live").read_text(encoding="utf-8") == "a file where the folder goes"


def test_begin_and_end_hold_a_file_for_code_that_cannot_wrap_its_work(home):
    begin_live("office", "mb-5", name="shop")
    assert _live(home)["mb-5"]["name"] == "shop"
    end_live("mb-5")
    assert _live(home) == {}


# ── what holds one ──────────────────────────────────────────────────────────


async def test_a_run_holds_its_file_naming_its_engine_model_and_current_step(
    tmp_path, home, monkeypatch
):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    seen: list[dict] = []

    def on_event(event):
        if isinstance(event, ToolCall):
            seen.append(_live(home)["mb-live"])

    result = await run_job(
        engine=wrap(ClaudeEngine), prompt="say metabot-probe", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), run_id="mb-live", on_event=on_event,
        meta={"ladder": "mb-ladder", "rung": 2, "parent": "mb-ladder.1"},
    )  # fmt: skip
    assert result.ok, result.error
    [entry] = seen
    assert (entry["kind"], entry["engine"], entry["current"]) == ("run", "claude", "step Bash")
    assert entry["model"] == "claude-haiku-5-5"  # as the engine said it started
    assert (entry["parent"], entry["name"]) == ("mb-ladder", "rung 2")
    assert _live(home) == {}


class Snaps:
    """An in-process engine that keeps what the live folder held while it ran."""

    name = "quick"
    tool_prefix = "quick"
    skip_tools = ()

    def __init__(self, home: Path) -> None:
        self.home = home
        self.seen: list[dict[str, dict]] = []

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        yield ToolCall("c1", "Read", {"path": "."})
        yield ToolResult("c1", "ok", True)
        self.seen.append(_live(self.home))
        yield Final("done", "s-1")


def test_a_ladder_holds_its_file_with_the_rung_it_is_on(tmp_path, home):
    from test_ladder import THREE, _fake_runs, _ladder

    run_rung, _ = _fake_runs(tmp_path, {1: {"ok": False, "error": "unreadable"}})
    seen: list[dict] = []

    async def snapping(rung, run_id, meta, cap, where=None):
        seen.append(_live(home)["L"])
        return await run_rung(rung, run_id, meta, cap, where)

    result = _ladder(THREE, snapping)
    assert result.answered
    assert [e["kind"] for e in seen] == ["ladder", "ladder"]
    assert [e["current"] for e in seen] == ["rung 1 of 3", "rung 2 of 3"]
    assert _live(home) == {}


async def test_an_office_run_holds_its_file_and_each_desk_run_names_it(tmp_path, home):
    from test_office_run import DeskEngine, _relay, _repo

    seen: list[dict[str, dict]] = []

    def snap(job):
        seen.append(_live(home))
        return "PLAN: write feature.txt"

    engines = {
        "claude": DeskEngine("claude", act=snap, calls=1),
        "codex": DeskEngine("codex"),
        "cursor": DeskEngine("cursor"),
    }
    result = await _relay(_repo(tmp_path), engines)
    office = result.office_run_id
    [held] = seen
    assert (held[office]["kind"], held[office]["name"]) == ("office", "shop")
    desk = held[f"{office}.plan"]
    assert (desk["kind"], desk["parent"], desk["name"]) == ("run", office, "desk plan")
    assert desk["current"] == "step Read"
    assert _live(home) == {}


async def test_a_routine_firing_holds_its_file_over_its_run(tmp_path, home):
    from metabot.routines import fire
    from test_serve import _save

    rid = _save(tmp_path, "0 9 * * *", name="nightly")
    engine = Snaps(home)
    firing = await fire(rid, make_engine=lambda name: engine)
    [held] = engine.seen
    routine = held[firing.firing_id]
    assert (routine["kind"], routine["name"], routine["engine"]) == (
        "routine", "nightly, not approved", "openai",
    )  # fmt: skip
    assert routine["current"] == f"run {firing.run_id}"
    assert held[firing.run_id]["parent"] == firing.firing_id
    assert _live(home) == {}


async def test_serve_holds_its_file_while_it_runs(tmp_path, home):
    from metabot.serve import serve
    from test_serve import _save, at, ticking

    _save(tmp_path, "0 9 * * *")
    engine = Snaps(home)
    clock = ticking(at(8, 59, 50), at(9, 0, 5))
    assert await serve(clock=clock, once=True, out=io.StringIO(), make_engine=lambda n: engine) == 0
    [held] = engine.seen
    assert (held["serve"]["kind"], held["serve"]["pid"]) == ("serve", os.getpid())
    assert _live(home) == {}


# ── alive or stale ──────────────────────────────────────────────────────────


def test_status_shows_what_runs_now_grouped_under_what_started_it(home, capsys):
    with (
        hold_live("ladder", "mb-L"),
        hold_live("run", "mb-L.2", parent="mb-L", name="rung 2", engine="claude", model="sonnet"),
    ):
        note_live("mb-L", current="rung 2 of 3")
        note_live("mb-L.2", current="step Bash")
        alive, stale = _status(capsys)
    assert stale == ""
    lines = alive.splitlines()
    assert lines[0].startswith("alive now") and "unsigned" in lines[0]
    ladder = next(line for line in lines if line.startswith("  ladder "))
    rung = next(line for line in lines if line.startswith("    run "))  # under its ladder
    assert ladder.startswith("  ladder mb-L  ") and "rung 2 of 3" in ladder
    assert rung.startswith("    run mb-L.2  ")
    for part in ("claude/sonnet", "rung 2", "step Bash", f"pid {os.getpid()}", "started"):
        assert part in rung
    assert re.search(r"running \d+s", rung)
    assert lines[-1].startswith("next: metabot receipts show")


def test_status_with_nothing_live_says_so(home, capsys):
    alive, stale = _status(capsys)
    assert alive.splitlines()[0] == "nothing is running" and stale == ""


def test_serve_is_named_once_on_its_line(home, capsys):
    with hold_live("serve", "serve"):
        alive, _stale = _status(capsys)
    assert alive.splitlines()[1].startswith(f"  serve  pid {os.getpid()}  started ")


def test_status_json_lists_the_live_and_the_stale_apart(home, capsys):
    gone = _dead_pid()
    _entry(home, "mb-gone", pid=gone)
    with hold_live("run", "mb-here", engine="claude", model="sonnet"):
        assert main(["status", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    [here] = shown["alive"]
    assert (here["id"], here["kind"], here["engine"], here["pid"]) == (
        "mb-here", "run", "claude", os.getpid(),
    )  # fmt: skip
    assert isinstance(here["elapsed_s"], int) and here["elapsed_s"] >= 0
    assert [(e["id"], e["pid"]) for e in shown["stale"]] == [("mb-gone", gone)]


def _dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_an_entry_whose_process_is_gone_is_stale_never_alive(home, capsys):
    _entry(home, "mb-gone", pid=_dead_pid(), current="step Edit")
    alive, stale = _status(capsys)
    assert "mb-gone" not in alive and alive.splitlines()[0] == "nothing is running"
    assert "mb-gone" in stale and "step Edit" in stale
    alive_now, gone = live_entries()
    assert alive_now == [] and [e["id"] for e in gone] == ["mb-gone"]


def test_a_process_killed_by_its_own_pid_leaves_its_entry_stale(home, capsys):
    code = (
        "import time\n"
        "from metabot.live import hold_live\n"
        "with hold_live('run', 'mb-held', engine='claude', model='sonnet'):\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(120)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        alive, stale = _status(capsys)
        assert "mb-held" in alive and f"pid {proc.pid}" in alive and stale == ""
        proc.kill()  # SIGKILL: no finally runs, so its file stays behind
        proc.wait(timeout=30)
        assert (home / "live" / "mb-held.json").is_file()
        alive, stale = _status(capsys)
        assert "mb-held" not in alive and "mb-held" in stale and f"pid {proc.pid}" in stale
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.mark.skipif(not PROC, reason="reads /proc")
def test_a_process_that_exited_but_is_not_yet_reaped_is_stale(home):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 30
        while _proc_stat(proc.pid)[0] != "Z":  # exited; its parent has not waited on it yet
            assert time.monotonic() < deadline
            time.sleep(0.01)
        _entry(home, "mb-zombie", pid=proc.pid, proc_start=int(_proc_stat(proc.pid)[19]))
        assert [e["id"] for e in live_entries()[1]] == ["mb-zombie"]
    finally:
        proc.wait()


@pytest.mark.skipif(not PROC, reason="reads /proc")
def test_an_entry_whose_pid_now_names_another_process_is_stale(home):
    began = int(_proc_stat(os.getpid())[19])
    _entry(home, "mb-reused", pid=os.getpid(), proc_start=began + 1)
    _entry(home, "mb-mine", pid=os.getpid(), proc_start=began)
    alive, stale = live_entries()
    assert [e["id"] for e in alive] == ["mb-mine"]
    assert [e["id"] for e in stale] == ["mb-reused"]


@pytest.fixture(params=["proc", "signal"])
def liveness(request, monkeypatch, tmp_path) -> str:
    """Each way MetaBot tells a live pid: ``/proc`` on Linux, a signal 0 where there is none."""
    if request.param == "signal":
        monkeypatch.setattr("metabot.live.PROC", tmp_path / "no-proc")
    elif not PROC:
        pytest.skip("reads /proc")
    return str(request.param)


@pytest.mark.parametrize(
    "pid",
    [0, -1, True, "1", 1.0, None, 2**64],  # 0 and -1 would signal a whole group; True is 1
)
def test_a_pid_that_names_no_one_process_is_never_alive(home, liveness, pid):
    _entry(home, "mb-odd", pid=pid)
    alive, stale = live_entries()
    assert alive == [] and [e["id"] for e in stale] == ["mb-odd"]


def test_either_way_a_live_pid_is_alive_and_a_gone_one_is_not(home, liveness):
    _entry(home, "mb-here", pid=os.getpid())
    _entry(home, "mb-gone", pid=_dead_pid())
    alive, stale = live_entries()
    assert [e["id"] for e in alive] == ["mb-here"] and [e["id"] for e in stale] == ["mb-gone"]


def test_a_live_file_that_does_not_read_is_stale_and_never_breaks_status(home, capsys):
    folder = home / "live"
    folder.mkdir(parents=True)
    (folder / "mb-text.json").write_text("{not json", encoding="utf-8")
    (folder / "mb-list.json").write_text("[1, 2]", encoding="utf-8")
    (folder / "mb-deep.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    (folder / "mb-bytes.json").write_bytes(b"\xff\xfe{")
    (folder / ".mb-1.json.tmp").write_text("{}", encoding="utf-8")  # a write cut short
    alive, stale = _status(capsys)
    assert alive.splitlines()[0] == "nothing is running"
    for name in ("mb-text", "mb-list", "mb-deep", "mb-bytes"):
        assert f"{name}  does not read" in stale
    assert ".mb-1" not in stale


def test_what_an_entry_says_is_printed_escaped(home, capsys):
    _entry(home, "mb-esc", current="step \x1b[2Jgone", engine="claude\x07")
    _entry(home, "mb-esc-gone", pid=_dead_pid(), name="desk \x1b]0;title\x07")
    alive, stale = _status(capsys)
    assert "mb-esc  " in alive and "mb-esc-gone" in stale
    for shown in (alive, stale):
        assert "\x1b" not in shown and "\x07" not in shown
    assert "\\x1b[2Jgone" in alive and "claude\\x07" in alive and "\\x1b]0;title" in stale
