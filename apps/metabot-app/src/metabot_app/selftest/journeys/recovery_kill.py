"""Recovery: the app is killed mid-run, the run with it (what a phone's OS does: no
shutdown, the whole app at once). The app comes back; the run is listed as interrupted
in Runs & Receipts (never shown live or done, never dropped, no receipt for it); the
run's own log says why; the next run works and its receipt verifies."""
import json
import os
import signal
import time
from pathlib import Path

from .. import fake_engine as fe

AXIS = "recovery"
NEEDS = ()                       # the app runs the core itself where no desk exists (no tmux, the phone)
BUDGET = {"the app comes back": 5_000}
SCENARIOS = {"slow_kill": [fe.answer("too late", delay_ms=6000)]}
LIVE_WAIT_S = 15


def live_pid(home: Path, run_id: str):
    """The pid the core's live file for ``run_id`` names; None when there is none."""
    try:
        entry = json.loads((home / "live" / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = entry.get("pid") if isinstance(entry, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return Path(f"/proc/{pid}/stat").is_file() and " Z " in Path(f"/proc/{pid}/stat").read_text()[-64:]


def run(s):
    J = "recovery_kill"
    s.set_model("slow_kill")
    box = {}

    def under_way():
        status, body = s.api("POST", "/api/run", {"task": "take your time", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "k1"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        box["desk"] = body["name"]
        deadline = time.monotonic() + LIVE_WAIT_S
        while time.monotonic() < deadline:
            status, idx = s.api("GET", "/api/mb/runs?limit=50")
            rows = [r for r in (idx.get("runs") or []) if status == 200 and r.get("desk") == box["desk"]]
            live = [r for r in rows if r.get("state") == "live" and r.get("run_id")]
            if live:
                box["run_id"] = live[0]["run_id"]
                box["pid"] = live_pid(s.home, box["run_id"])
                assert box["pid"], f"{box['run_id']}: no live file with a pid"
                return {"_report": True, "value": {"run_id": box["run_id"]}, "run_id": box["run_id"]}
            assert not any(r.get("state") == "done" for r in rows), "the run ended before it could be killed"
            time.sleep(0.25)
        raise AssertionError(f"{box['desk']} was never live in {LIVE_WAIT_S} s")
    if not s.step(J, "a run is under way", under_way):
        s.set_model("ask")
        return

    def killed():
        s.kill_server()                                   # SIGKILL, no shutdown
        try:
            os.kill(box["pid"], signal.SIGKILL)           # the run dies with the app
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_gone(box["pid"]):
            time.sleep(0.05)
        assert pid_gone(box["pid"]), f"pid {box['pid']} still runs"
        status, _ = s.api("GET", "/api/meta")
        assert status != 200, "the server still answers"
        return {"_report": True, "value": {"pid": box["pid"]}}
    s.step(J, "the app is killed mid-run", killed)

    def back():
        ms = s.start_server()
        return {"_report": True, "value": {"ms": round(ms)}}
    s.step(J, "the app comes back", back, budget_ms=BUDGET["the app comes back"])

    def listed():
        status, idx = s.api("GET", "/api/mb/runs?limit=50")
        assert status == 200, f"/api/mb/runs {status}"
        rows = [r for r in (idx.get("runs") or []) if r.get("run_id") == box["run_id"]]
        assert len(rows) == 1, f"the run is listed {len(rows)} times: {rows}"
        r = rows[0]
        assert r.get("state") == "interrupted", f"listed as {r.get('state')!r}, not interrupted"
        assert "receipt" in (r.get("problem") or ""), f"no plain reason: {r.get('problem')!r}"
        assert not s.receipt_path(box["run_id"]).exists(), "a killed run must not have a receipt"
        return {"_report": True, "value": r, "run_id": box["run_id"]}
    s.step(J, "the interrupted run is listed as such", listed)

    if not s.caps.get("desks"):     # the app ran the core itself: its log is the record
        def log_says():
            status, body = s.api("GET", f"/api/mb/runlog/{box['desk']}")
            assert status == 200, f"/api/mb/runlog {status}"
            assert body.get("alive") is False, "the run is reported alive"
            assert body.get("exit") == 255, f"exit {body.get('exit')!r}, not 255 (interrupted)"
            assert "interrupted" in (body.get("log") or ""), "the log does not say interrupted"
            return {"_report": True, "value": {"exit": body.get("exit")}}
        s.step(J, "the run's log says why", log_says)

    s.set_model("ask")

    def next_run():
        status, body = s.api("POST", "/api/run", {"task": "hello again", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "k2"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        row = s.wait_run(body["name"])
        run_id = row["run_id"]
        ok, sha = s.verify_cli(s.receipt_path(run_id))
        assert ok, f"{run_id}: metabot receipts verify did not say VALID"
        return {"_report": True, "value": {"run_id": run_id}, "run_id": run_id, "receipt_sha256": sha}
    s.step(J, "the next run works", next_run)
