"""A run started from the app tells its own lane how it ended (11-09 tablet walk:
the run finished in 5 s with a valid receipt and the lane still said only "run
started"). The server links each run desk to its MetaBot run id, from the live
list while it runs or from the receipt line it printed once it has ended, then
posts the answer (or the failure) with a short receipt summary, once. Finished
runs carry their desk, so "Open desk" works from the receipt viewer."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Env, server  # noqa: E402
from metabot_app import mb  # noqa: E402


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.cwd = str(self.env.projects / "alpha")
        self.procs = []
        with server._local_lock:
            server._local.clear()

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.env.stop()

    def start_run(self, engine="openai"):
        os.environ["FAKE_PANE_PID"] = str(os.getpid())   # the test process is the run desk's pane
        try:
            s, j = self.env.post("/api/run", {"task": "count the files", "engine": engine, "cwd": self.cwd})
        finally:
            os.environ.pop("FAKE_PANE_PID")
        self.assertEqual(s, 200, j)
        return j["name"]

    def live(self, run_id):
        child = subprocess.Popen(["sleep", "30"])   # a child of the pane: the live run's pid
        self.procs.append(child)
        d = self.env.mbhome / "live"
        d.mkdir(exist_ok=True)
        (d / f"{run_id}.json").write_text(json.dumps({"format": "metabot.live/1", "kind": "run", "id": run_id,
                                                      "pid": child.pid, "proc_start": 1, "started": "2026-01-01T00:00:00Z",
                                                      "engine": "openai", "name": None, "parent": None,
                                                      "model": None, "current": "thinking"}))
        return child

    def end(self, child, run_id):
        child.kill()
        child.wait()
        (self.env.mbhome / "live" / f"{run_id}.json").unlink()

    def row(self, run_id, receipt=True, **kw):
        r = {"run_id": run_id, "cwd": self.cwd, "engine": "openai", "model": "small", "status": "ok",
             "final": "There are three files.", "steps": 1, "tool_calls": 1, "tokens": 612,
             "cost_musd": 0, "cost_source": "local", "error": None, "login": None}
        r.update(kw)
        with open(self.env.mbhome / "runs.jsonl", "a") as f:
            f.write(json.dumps(r) + "\n")
        if receipt:
            (self.env.mbhome / "receipts").mkdir(exist_ok=True)
            (self.env.mbhome / "receipts" / f"{run_id}.json").write_text("{}")

    def posted(self):
        with server._local_lock:
            return [e for e in server._local if e.get("run") or e.get("via") == "alert"]

    def tick(self):
        server.invalidate()
        server.run_report_once()

    def test_a_finished_run_posts_its_answer_in_its_own_lane_once(self):
        name = self.start_run()
        child = self.live("mbone")
        self.tick()
        self.assertEqual(mb.registry(server.MB)[name].get("run_id"), "mbone", "linked while it runs")
        self.assertEqual(self.posted(), [], "nothing is told while it runs")
        self.end(child, "mbone")
        self.row("mbone")
        self.tick()
        ev = self.posted()
        self.assertEqual(len(ev), 1, ev)
        ev = ev[0]
        self.assertEqual((ev["target"], ev["from"], ev["via"], ev["typed"], ev["mirror"]), (name, name, "desk", False, True))
        self.assertEqual(ev["message"], "There are three files.")
        self.assertEqual(ev["run"]["id"], "mbone")
        self.assertEqual(ev["run"]["receipt_state"], "valid")
        self.assertEqual((ev["run"]["steps"], ev["run"]["cost_musd"], ev["run"]["cost_source"], ev["run"]["status"]),
                         (1, 0, "local", "ok"))
        self.tick()
        self.assertEqual(len(self.posted()), 1, "told once")
        s, j = self.env.get("/api/mb/runs")
        r = [x for x in j["runs"] if x["run_id"] == "mbone"][0]
        self.assertEqual((r["state"], r["desk"]), ("done", name), "a finished run keeps its desk")

    def test_a_run_too_fast_to_be_seen_live_is_linked_from_its_receipt_line(self):
        name = self.start_run()
        self.row("mbfast")
        self.env.set_pane(f"fc-{name}", "$ metabot ask -e openai - <prompt>\n[metabot] started (small)\n"
                          "There are three files.\n"
                          f"[metabot] receipt /work/u/.metabot/receipts/mbfast.json (valid, 1 steps)\n"
                          "[metabot] cost $0.00: the model runs on this machine\n\n[metachat] exit 0\n$ ")
        self.tick()
        ev = self.posted()
        self.assertEqual([(e["target"], e["run"]["id"]) for e in ev], [(name, "mbfast")])

    def test_the_receipt_line_is_read_only_after_the_run_has_ended(self):
        name = self.start_run()
        self.row("mbearly")
        self.env.set_pane(f"fc-{name}", "[metabot] receipt /x/receipts/mbearly.json (valid, 1 steps)\n")   # no exit line yet
        self.tick()
        self.assertEqual(self.posted(), [])
        self.assertIsNone(mb.registry(server.MB)[name].get("run_id"))

    def test_a_failed_run_says_why(self):
        name = self.start_run(engine="claude")
        child = self.live("mbfail")
        self.tick()
        self.end(child, "mbfail")
        self.row("mbfail", status="error", steps=0, final="Failed to authenticate: this login reached its time limit",
                 error="claude: Failed to authenticate: this login reached its time limit")
        self.tick()
        ev = self.posted()
        self.assertEqual(len(ev), 1, ev)
        self.assertTrue(ev[0]["message"].startswith("The run failed: "), ev[0]["message"])
        self.assertIn("time limit", ev[0]["message"])
        self.assertEqual(ev[0]["run"]["status"], "error")

    def test_a_refused_key_says_to_check_the_key(self):
        name = self.start_run(engine="openai")
        child = self.live("mbkey")
        self.tick()
        self.end(child, "mbkey")
        self.row("mbkey", status="error", steps=0, final="",
                 error='openai: http://127.0.0.1:1/v1 answered 401: {"error": {"message": "bad key"}}')
        self.tick()
        ev = self.posted()
        self.assertEqual(len(ev), 1, ev)
        self.assertTrue(ev[0]["message"].startswith("The run failed: "), ev[0]["message"])
        self.assertIn("answered 401", ev[0]["message"])
        self.assertTrue(ev[0]["message"].endswith(" Check this engine's key in Settings → Accounts."),
                        ev[0]["message"])

    def test_a_run_that_ended_with_no_receipt_says_so(self):
        name = self.start_run()
        self.env.set_pane(f"fc-{name}", "$ metabot ask -e nope - <prompt>\nmetabot: no engine nope\n\n[metachat] exit 2\n$ ")
        self.tick()
        ev = self.posted()
        self.assertEqual(len(ev), 1, ev)
        self.assertEqual((ev[0]["via"], ev[0]["target"]), ("alert", name))
        self.assertIn("exit 2", ev[0]["message"])
        self.assertIn("no receipt", ev[0]["message"])
        self.tick()
        self.assertEqual(len(self.posted()), 1, "told once")

    def test_a_long_answer_is_cut(self):
        name = self.start_run()
        child = self.live("mblong")
        self.tick()
        self.end(child, "mblong")
        self.row("mblong", final="x" * (server.MIRROR_TEXT_MAX + 500))
        self.tick()
        ev = self.posted()[0]
        self.assertLessEqual(len(ev["message"]), server.MIRROR_TEXT_MAX + 40)

    def test_run_desks_from_before_the_window_are_left_alone(self):
        name = self.start_run()
        reg = mb.registry(server.MB)[name]
        reg["created"] = int(time.time()) - server.RUN_REPORT_WINDOW - 60
        mb.reg_write(server.MB, reg)
        self.row("mbold")
        self.env.set_pane(f"fc-{name}", "[metabot] receipt /x/receipts/mbold.json (valid, 1 steps)\n\n[metachat] exit 0\n$ ")
        self.tick()
        self.assertEqual(self.posted(), [])

    def test_the_stream_carries_the_run_summary(self):
        from test_hotfix import open_stream, read_event
        s = open_stream(self.env)
        try:
            server.local_event("done", "run-9", "run-9", via="desk", typed=False, mirror=True,
                               run={"id": "mbx", "receipt_state": "valid", "steps": 1})
            ev = read_event(s)
            while ev.get("app"):
                ev = read_event(s)
            self.assertEqual(ev.get("run"), {"id": "mbx", "receipt_state": "valid", "steps": 1})
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
