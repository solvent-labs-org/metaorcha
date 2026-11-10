"""Speed (11-09): every MetaBot read used to start a fresh python that imported
the whole runtime (mbq runs 9.7 s, live 1.8 s on the PC). mbq.py serve keeps one
warm process answering JSON lines; mbqw.Worker is its client: one request at a
time, a timeout that kills and restarts, a maximum age so a MetaBot upgrade is
picked up. Receipt states are memoised per receipt and journal file stat."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "src" / "metabot_app"
sys.path.insert(0, str(HERE.parent / "src"))
from metabot_app import mbqw  # noqa: E402

FAKEMB = HERE / "fixtures" / "fakemb"


def env_for(home):
    env = dict(os.environ, METABOT_HOME=str(home), PYTHONPATH=str(FAKEMB))
    return env


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="mbqw-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / "receipts").mkdir()
        (self.home / "runs").mkdir()
        (self.home / "runs.jsonl").write_text("".join(json.dumps({"run_id": f"r{i}"}) + "\n" for i in range(3)))
        for i in range(2):
            (self.home / "receipts" / f"r{i}.json").write_text("{}")
        self.w = mbqw.Worker([sys.executable, str(ROOT / "mbq.py"), "serve"], env=env_for(self.home),
                             cwd=str(self.home), timeout=10)

    def tearDown(self):
        self.w.close()

    def calls(self):
        p = self.home / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_ping_and_one_process_for_many_reads(self):
        self.assertEqual(self.w.call(["ping"]), {"pong": True})
        pid = self.w.pid
        for _ in range(5):
            self.assertEqual(self.w.call(["ping"]), {"pong": True})
        self.assertEqual(self.w.pid, pid)

    def test_runs_memoise_receipt_states_until_a_file_changes(self):
        a = self.w.call(["runs"])
        self.assertEqual([r["state"] for r in a["rows"]], ["no receipt", "valid", "valid"])
        self.assertEqual(len(self.calls()), 3)
        b = self.w.call(["runs"])
        self.assertEqual(b, a)
        self.assertEqual(len(self.calls()), 3)            # no second check of an unchanged receipt
        time.sleep(0.01)
        (self.home / "receipts" / "r2.json").write_text("{}")   # r2 gains its receipt
        c = self.w.call(["runs"])
        self.assertEqual(c["rows"][0]["state"], "valid")
        self.assertEqual(len(self.calls()), 4)            # only r2 is read again

    def test_a_bad_request_answers_an_error_and_the_worker_lives_on(self):
        pid = self.w.call(["ping"]) and self.w.pid
        self.assertIn("error", self.w.call(["nope"]))
        self.assertEqual(self.w.call(["ping"]), {"pong": True})
        self.assertEqual(self.w.pid, pid)

    def test_a_malformed_line_answers_an_error(self):
        out = subprocess.run([sys.executable, str(ROOT / "mbq.py"), "serve"], input="not json\n",
                             capture_output=True, text=True, env=env_for(self.home), timeout=10)
        self.assertIn("error", json.loads(out.stdout.splitlines()[0]))


class WorkerTests(unittest.TestCase):
    """The client against small scripts standing in for the worker."""

    def script(self, body):
        d = Path(tempfile.mkdtemp(prefix="mbqw-s-"))
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "w.py"
        p.write_text("import json, sys, time\n" + body)
        return [sys.executable, str(p)]

    def test_a_hung_worker_times_out_is_killed_and_restarts(self):
        w = mbqw.Worker(self.script("for line in sys.stdin:\n    time.sleep(30)\n"), timeout=0.5)
        t0 = time.time()
        r = w.call(["x"])
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(r, {"error": "mbq: timeout"})
        self.assertIsNone(w.pid)                          # killed; the next call starts a new one
        w.close()

    def test_a_dead_worker_is_restarted(self):
        body = ("for line in sys.stdin:\n    q = json.loads(line)\n"
                "    print(json.dumps({'id': q['id'], 'result': 'ok'}), flush=True)\n    sys.exit(0)\n")
        w = mbqw.Worker(self.script(body), timeout=5)
        self.assertEqual(w.call(["a"]), "ok")
        self.assertEqual(w.call(["b"]), "ok")             # the first process exited; a second answered
        w.close()

    def test_an_old_worker_is_replaced(self):
        body = ("import os\nfor line in sys.stdin:\n    q = json.loads(line)\n"
                "    print(json.dumps({'id': q['id'], 'result': os.getpid()}), flush=True)\n")
        w = mbqw.Worker(self.script(body), timeout=5, max_age=0.2)
        p1 = w.call(["a"])
        time.sleep(0.3)
        p2 = w.call(["b"])
        self.assertNotEqual(p1, p2)
        w.close()

    def test_a_worker_that_cannot_start_answers_an_error(self):
        w = mbqw.Worker(["/nonexistent/python", "x"], timeout=1)
        self.assertEqual(w.call(["a"])["error"][:4], "mbq:")
        w.close()


if __name__ == "__main__":
    unittest.main()
