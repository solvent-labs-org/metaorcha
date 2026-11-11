"""4.13 + section 7: live runs resolve to desks through /proc; needs_you."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import mb  # noqa: E402


class RunsTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_a_run_whose_process_died_is_listed_as_interrupted(self):
        # the app (and the run with it) was killed mid-run: the core left a live file for a
        # pid that is gone and never sealed a receipt. Runs & Receipts says so; it is not
        # shown as live, not as done, and never silently dropped.
        gone = subprocess.Popen(["true"])
        gone.wait()
        live = self.env.mbhome / "live"
        live.mkdir()
        (live / "mb-dead-1.json").write_text(json.dumps({"format": "metabot.live/1", "kind": "run",
                                                         "id": "mb-dead-1", "pid": gone.pid,
                                                         "proc_start": 1, "engine": "openai",
                                                         "started": "2026-01-01T00:00:00Z"}))
        s, j = self.env.get("/api/mb/runs")
        self.assertEqual(s, 200)
        rows = [r for r in j["runs"] if r["run_id"] == "mb-dead-1"]
        self.assertEqual(len(rows), 1, j["runs"])
        r = rows[0]
        self.assertEqual(r["state"], "interrupted")
        self.assertEqual(r["engine"], "openai")
        self.assertEqual(r["started"], "2026-01-01T00:00:00Z")
        self.assertIn("no receipt", r["problem"])
        self.assertNotIn("receipt_state", r)
        s, j = self.env.get("/api/tree")
        self.assertEqual(s, 200)
        self.assertIn('"state": "interrupted"', json.dumps(j))

    def test_live_run_resolves_desk_and_needs_you(self):
        cwd = str(self.env.projects / "alpha")
        os.environ["FAKE_PANE_PID"] = str(os.getpid())   # the test process is the "pane"
        try:
            s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
        finally:
            os.environ.pop("FAKE_PANE_PID")
        self.assertEqual(s, 200, j)
        child = subprocess.Popen(["sleep", "30"])   # a child of the pane: the live run's pid
        try:
            live = self.env.mbhome / "live"
            live.mkdir()
            (live / "live-1.json").write_text(json.dumps({"format": "metabot.live/1", "kind": "run",
                                                           "id": "live-1", "pid": child.pid,
                                                           "proc_start": 1, "started": "2026-01-01T00:00:00Z",
                                                           "engine": "claude", "name": None, "parent": None,
                                                           "model": None, "current": "thinking"}))
            self.env.set_pane("fc-run-1", "\n".join(["old line"] * 50 + ["Edit file.py", "allow? type y", ""]))
            s, j = self.env.get("/api/mb/runs")
            self.assertEqual(s, 200)
            self.assertEqual(j["total"], 0)
            r = j["runs"][0]
            self.assertEqual(r["state"], "live")
            self.assertEqual(r["desk"], "run-1")
            self.assertEqual(r["run_id"], "live-1")
            self.assertEqual(r["live_id"], "live-1")
            self.assertTrue(r["needs_you"])
            self.assertEqual(r["project"], "alpha")
            self.assertEqual(r["cwd"], cwd)
            self.assertEqual(r["engine"], "claude")
            cap = [c for c in self.env.calls("tmux") if c[:1] == ["capture-pane"]][-1]
            self.assertEqual(cap, ["capture-pane", "-p", "-t", "=fc-run-1:"])
            s, j = self.env.get("/api/desks")
            d = [x for x in j["desks"] if x["name"] == "run-1"][0]
            self.assertTrue(d["needs_you"])
            self.assertEqual(d["meta"]["kind"], "run")
            # the prompt scrolled away: no longer needs the person (after the 2 s cache)
            self.env.set_pane("fc-run-1", "allow? type y\n" + "\n".join(["more"] * 6))
            time.sleep(2.1)
            s, j = self.env.get("/api/mb/runs")
            self.assertFalse(j["runs"][0]["needs_you"])
        finally:
            child.kill()
            child.wait()
        s, j = self.env.get("/api/mb/runs")
        # the process is gone and sealed nothing: the entry is stale, so the run is not live;
        # it is listed as interrupted (never dropped). Its desk cannot be walked from a dead
        # pid, so a tmux run's desk is unknown here; a run the app spawned itself keeps it.
        self.assertEqual([(r["state"], r["run_id"], r["desk"]) for r in j["runs"]],
                         [("interrupted", "live-1", None)])

    def test_runs_limit_and_error(self):
        for i in range(5):
            with open(self.env.mbhome / "runs.jsonl", "a") as f:
                f.write(json.dumps({"run_id": f"r{i}", "cwd": "/x"}) + "\n")
        s, j = self.env.get("/api/mb/runs?limit=3")
        self.assertEqual([r["run_id"] for r in j["runs"]], ["r4", "r3", "r2"])
        self.assertEqual(j["total"], 5)
        s, j = self.env.get("/api/mb/runs?limit=0")
        self.assertEqual(len(j["runs"]), 1)
        s, j = self.env.get("/api/mb/runs?limit=999")
        self.assertEqual(len(j["runs"]), 5)
        os.environ["FAKE_MBQ_FAIL"] = "1"
        try:
            s, j = self.env.get("/api/mb/runs")
        finally:
            os.environ.pop("FAKE_MBQ_FAIL")
        self.assertEqual(s, 200)
        self.assertEqual(j["runs"], [])
        self.assertEqual(j["error"], "RuntimeError: boom")

    def test_a_finished_run_desk_asks_nothing(self):
        # 11-11 (found on the tablet): between the run script's exit line and the
        # shell's first prompt the screen is the model's answer, which may quote an
        # ask; needs_you read true for one tick and the page opened the prompt sheet
        # over a finished run. An exit line among the last six lines with no command
        # typed after it means nothing waits.
        s, j = self.env.post("/api/spawn", {"kind": "claude", "label": "needs"})
        self.assertEqual(s, 200, j)
        head = "\n".join(["old line"] * 50) + "\n"
        ended = ["Do you want to proceed with the second part?\n"
                 + "[metabot] receipt /x/r.json (valid, 4 steps)\n[metachat] exit 0\n",
                 "Allow command?\n[metachat] exit 1\nTo run a command as administrator, use sudo.\n"
                 + "See man sudo_root for details.\n"]
        for text in ended:
            self.env.set_pane("fc-needs", head + text)
            server.MB._needs.clear()
            self.assertFalse(mb.needs_you(server.MB, "needs"), text)
        # a command typed into the shell that follows the run asks again
        self.env.set_pane("fc-needs", head + "[metachat] exit 0\nuser@host:~/proj$ sudo ls\n"
                                             "[sudo] password for user:\n")
        server.MB._needs.clear()
        self.assertTrue(mb.needs_you(server.MB, "needs"))

    def test_needs_you_patterns_from_the_last_six_lines(self):
        # 21.4 / server-flows-5: each CLI's prompt on a registered desk; a shell
        # prompt as the last line means nothing waits
        s, j = self.env.post("/api/spawn", {"kind": "claude", "label": "needs"})
        self.assertEqual(s, 200, j)
        waiting = ["Do you want to proceed?\n  1. Yes\n  2. No", "Run this command? [y/N]", "Allow command?",
                   "[sudo] password for user:", "Esc to cancel", "Edit file.py\nallow? type y",
                   "type 'yes' to continue", "Overwrite? (y/N)", "Allow once?"]
        for text in waiting:
            self.env.set_pane("fc-needs", "\n".join(["old line"] * 50) + "\n" + text + "\n")
            server.MB._needs.clear()
            server.invalidate()
            self.assertTrue(mb.needs_you(server.MB, "needs"), text)
            s, j = self.env.get("/api/desks?scope=metachat")
            self.assertTrue(j["desks"][0]["needs_you"], text)
        not_waiting = ["Do you want to proceed?\n 1. Yes\nuser@host:~/proj$ ",          # back at a shell
                       "Allow command?\nroot@host:/# ",
                       "allowed tools: Read, Edit",                                      # no question
                       "Do you want to proceed?\n" + "\n".join(["more"] * 6),            # scrolled off (7th line)
                       "Reading the file...", ""]
        for text in not_waiting:
            self.env.set_pane("fc-needs", "\n".join(["old line"] * 50) + "\n" + text + "\n")
            server.MB._needs.clear()
            server.invalidate()
            self.assertFalse(mb.needs_you(server.MB, "needs"), text)
        # the sixth line still counts
        self.env.set_pane("fc-needs", "Do you want to proceed?\n" + "\n".join(["more"] * 5))
        server.MB._needs.clear()
        self.assertTrue(mb.needs_you(server.MB, "needs"))
        # the ask class and since (22.3 waiting list)
        server.MB._needs.clear()
        server.MB._needs_since.clear()
        self.env.set_pane("fc-needs", "Bash command\nrm -rf build/\nDo you want to proceed?\n 1. Yes")
        info = mb.needs_info(server.MB, "needs")
        self.assertEqual((info["needs"], info["cls"]), (True, "destructive"))
        self.assertIsInstance(info["since"], float)
        since = info["since"]
        server.MB._needs.clear()
        self.assertEqual(mb.needs_info(server.MB, "needs")["since"], since)   # the same wait, not a new one
        server.MB._needs.clear()
        self.env.set_pane("fc-needs", "Edit file.py\nDo you want to proceed?\n 1. Yes")
        self.assertEqual(mb.needs_info(server.MB, "needs")["cls"], "write")
        server.MB._needs.clear()
        self.env.set_pane("fc-needs", "Read file.py\nDo you want to proceed?\n 1. Yes")
        self.assertEqual(mb.needs_info(server.MB, "needs")["cls"], "read")
        server.MB._needs.clear()
        self.env.set_pane("fc-needs", "done\nuser@host:~$ ")
        info = mb.needs_info(server.MB, "needs")
        self.assertEqual((info["needs"], info["since"]), (False, None))

    def test_desk_of_pid_walk_limit(self):
        self.assertIsNone(mb.desk_of_pid(server.MB, 0))
        self.assertIsNone(mb.desk_of_pid(server.MB, "x"))
        self.assertIsNone(mb.desk_of_pid(server.MB, os.getpid()))   # no registered desk

    def test_mbq_pins_home(self):
        self.env.clear_calls()
        mb.mbq(server.MB, "state", "abc")
        call = self.env.calls("mbpython")[-1]
        self.assertEqual(call[1:], ["state", "abc"])
        self.assertTrue(call[0].endswith("/mbq.py"))


if __name__ == "__main__":
    unittest.main()
