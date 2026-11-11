"""T2/E3: the run path without tmux (a Linux box with no tmux, or the phone). The app
spawns ``metabot ask`` itself: the prompt on stdin, stdout and stderr into a log under
the state folder, the environment scrubbed as a desk's is, in its own session so one
kill ends the whole tree. A journey's assertions stay index-based; only "a terminal
exists" is conditional on ``caps.desks``."""
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import mb, tmuxcmd  # noqa: E402


def wait_for(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class ProcessRunTests(unittest.TestCase):
    def setUp(self):
        self.flag = mock.patch.dict(os.environ, {"METABOT_ON_DEVICE": "1"})
        self.flag.start()
        self.env = Env(config=self.config()).start()
        self.cwd = str(self.env.projects / "alpha")
        with server._local_lock:
            server._local.clear()

    def config(self):
        return {}

    def tearDown(self):
        for e in mb.registry(server.MB).values():
            if isinstance(e.get("pid"), int):
                try:
                    os.killpg(e["pid"], signal.SIGKILL)
                except OSError:
                    pass
        self.env.stop()
        self.flag.stop()

    def start(self, **body):
        b = {"task": "count the files", "engine": "openai", "cwd": self.cwd}
        b.update(body)
        s, j = self.env.post("/api/run", b)
        self.assertEqual(s, 200, j)
        return j["name"]

    def log(self, name):
        return self.env.state / "runs" / f"{name}.log"

    def test_a_run_without_tmux_is_a_child_process_with_a_log(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        try:
            name = self.start()
        finally:
            os.environ.pop("FAKE_SLEEP")
        self.assertEqual([c for c in self.env.calls("tmux") if c[:1] == ["new-session"]], [])
        e = mb.reg_read(server.MB, name)
        self.assertEqual(e["kind"], "run")
        self.assertIsInstance(e["pid"], int)
        self.assertEqual(e["log"], f"{name}.log")
        self.assertTrue(self.log(name).is_file())
        self.assertEqual(self.log(name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((self.env.state / "prompts").glob("*.txt")), [])   # the child holds it
        self.assertTrue(mb.pid_alive(e["pid"]))
        self.assertTrue(wait_for(lambda: "prompt: count the files" in self.log(name).read_text()))
        call = [c for c in self.env.calls("metabot") if c[:2] == ["ask", "-e"]][-1]
        self.assertEqual(call, ["ask", "-e", "openai", "--cwd", self.cwd, "--read-only", "-"])
        self.assertTrue(wait_for(lambda: not mb.pid_alive(e["pid"])))

    def test_the_child_gets_the_scrubbed_environment(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        try:
            name = self.start()
        finally:
            os.environ.pop("ANTHROPIC_API_KEY")
        self.assertTrue(wait_for(lambda: "key: unset" in self.log(name).read_text()))
        self.assertIn(f"home: {self.env.mbhome}", self.log(name).read_text())

    def test_a_cursor_account_reaches_the_child_process(self):
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        name = self.start(engine="cursor", account="alt")
        folder = self.env.state / "accounts/cursor/alt"
        self.assertTrue(wait_for(lambda: f"xdg: {folder}/config" in self.log(name).read_text()))

    def test_a_live_process_run_links_to_its_desk(self):
        os.environ["FAKE_SLEEP"] = "3"
        try:
            name = self.start()
        finally:
            os.environ.pop("FAKE_SLEEP")
        pid = mb.reg_read(server.MB, name)["pid"]
        d = self.env.mbhome / "live"
        d.mkdir(exist_ok=True)
        (d / "live-p1.json").write_text(json.dumps({"format": "metabot.live/1", "kind": "run", "id": "live-p1",
                                                    "pid": pid, "proc_start": 1, "started": "2026-01-01T00:00:00Z",
                                                    "engine": "openai", "name": None, "parent": None,
                                                    "model": None, "current": "thinking"}))
        s, j = self.env.get("/api/mb/runs")
        self.assertEqual(s, 200)
        r = j["runs"][0]
        self.assertEqual((r["state"], r["desk"], r["run_id"]), ("live", name, "live-p1"))
        self.assertFalse(r["needs_you"])

    def test_a_finished_run_reports_its_exit_and_its_receipt(self):
        os.environ["FAKE_ASK_RECEIPT"] = "1"
        try:
            name = self.start()
        finally:
            os.environ.pop("FAKE_ASK_RECEIPT")
        self.assertTrue(wait_for(lambda: mb.run_pane_end(server.MB, name)[0] is not None))
        code, rid = mb.run_pane_end(server.MB, name)
        self.assertEqual(code, 0)
        self.assertTrue(rid and rid.startswith("ask-"), rid)
        self.assertTrue(self.log(name).read_text().rstrip().endswith("[metachat] exit 0"))
        server.invalidate()
        server.run_report_once()
        with server._local_lock:
            told = [e for e in server._local if e.get("run")]
        self.assertEqual(len(told), 1, told)
        self.assertEqual(told[0]["run"]["id"], rid)
        s, j = self.env.get(f"/api/mb/runlog/{name}")
        self.assertEqual(s, 200, j)
        self.assertEqual((j["exit"], j["alive"]), (0, False))
        self.assertIn("[metabot] receipt", j["log"])

    def test_a_failed_run_says_its_log_says_why(self):
        os.environ["FAKE_EXIT"] = "3"
        try:
            name = self.start()
        finally:
            os.environ.pop("FAKE_EXIT")
        self.assertTrue(wait_for(lambda: mb.run_pane_end(server.MB, name)[0] is not None))
        server.invalidate()
        server.run_report_once()
        with server._local_lock:
            alerts = [e["message"] for e in server._local if e.get("via") == "alert"]
        self.assertEqual(alerts, ["The run ended (exit 3) with no receipt; its log says why."])
        s, j = self.env.get(f"/api/mb/runlog/{name}")
        self.assertEqual((s, j["exit"]), (200, 3))
        self.assertIn("[metachat] exit 3", j["log"])

    def test_kill_ends_the_whole_process_tree(self):
        os.environ["FAKE_SLEEP"] = "30"
        try:
            name = self.start()
        finally:
            os.environ.pop("FAKE_SLEEP")
        pid = mb.reg_read(server.MB, name)["pid"]
        self.assertTrue(mb.pid_alive(pid))
        s, j = self.env.post("/api/kill", {"name": name, "confirm": True})
        self.assertEqual(s, 200, j)
        self.assertTrue(wait_for(lambda: not mb.pid_alive(pid), 4))
        self.assertIsNone(mb.reg_read(server.MB, name))
        self.assertEqual(self.env.get(f"/api/mb/runlog/{name}")[0], 404)

    def test_runlog_refuses_a_bad_name_and_an_unknown_one(self):
        self.assertEqual(self.env.get("/api/mb/runlog/..%2Fx")[0], 400)
        self.assertEqual(self.env.get("/api/mb/runlog/run-77")[0], 404)


class ProcessRunCapTests(ProcessRunTests):
    def config(self):
        return {"max_runs": 1}

    def test_the_run_cap_counts_a_live_process_run(self):
        os.environ["FAKE_SLEEP"] = "2"
        try:
            name = self.start()
            s, j = self.env.post("/api/run", {"task": "again", "engine": "openai", "cwd": self.cwd})
        finally:
            os.environ.pop("FAKE_SLEEP")
        self.assertEqual((s, j["error"]), (409, "cap"))
        pid = mb.reg_read(server.MB, name)["pid"]
        self.assertTrue(wait_for(lambda: not mb.pid_alive(pid)))
        s, j = self.env.post("/api/run", {"task": "again", "engine": "openai", "cwd": self.cwd})
        self.assertEqual(s, 200, j)

    # the inherited tests run again under max_runs=1; one at a time is what they do
    test_a_run_without_tmux_is_a_child_process_with_a_log = None
    test_the_child_gets_the_scrubbed_environment = None
    test_a_live_process_run_links_to_its_desk = None
    test_a_finished_run_reports_its_exit_and_its_receipt = None
    test_a_failed_run_says_its_log_says_why = None
    test_kill_ends_the_whole_process_tree = None
    test_runlog_refuses_a_bad_name_and_an_unknown_one = None


class NoTmuxOnLinuxTests(unittest.TestCase):
    """The same path when tmux is simply missing, without the phone flag."""

    def test_no_tmux_means_a_process_run(self):
        env = Env().start()
        try:
            with mock.patch.object(tmuxcmd, "found", return_value=None):
                s, j = env.post("/api/run", {"task": "x", "engine": "openai", "cwd": str(env.projects / "alpha")})
            self.assertEqual(s, 200, j)
            e = mb.reg_read(server.MB, j["name"])
            self.assertIsInstance(e["pid"], int)
            self.assertEqual([c for c in env.calls("tmux") if c[:1] == ["new-session"]], [])
            self.assertTrue(wait_for(lambda: not mb.pid_alive(e["pid"])))
        finally:
            env.stop()


if __name__ == "__main__":
    unittest.main()
