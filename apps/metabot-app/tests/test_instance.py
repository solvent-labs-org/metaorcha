"""A6: one server per state folder. STATE/server.json holds {pid, port, build} under an
fcntl lock. A second ``serve`` on the same state finds the first one and exits 0 saying
its port; a configured port taken by something else is left alone and a free port is
taken and recorded. The runner and the Android shell read server.json for the port."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, ROOT  # noqa: E402
from metabot_app import instance  # noqa: E402


class LockTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.addCleanup(self.env.stop)
        self.state = self.env.state

    def test_second_acquire_fails_and_release_frees(self):
        a = instance.Instance(self.state)
        b = instance.Instance(self.state)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_record_read_running(self):
        a = instance.Instance(self.state)
        self.assertTrue(a.acquire())
        a.record(8471, "abc1234")
        f = self.state / "server.json"
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        rec = instance.Instance(self.state).read()
        self.assertEqual(rec, {"pid": os.getpid(), "port": 8471, "build": "abc1234"})
        self.assertEqual(instance.Instance(self.state).running()["port"], 8471)
        a.release()
        self.assertFalse(f.exists())           # release removes the record
        # a stale record from a dead process is not "running"
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
        f.write_text(json.dumps({"pid": int(dead.stdout), "port": 1, "build": "x"}))
        self.assertIsNone(instance.Instance(self.state).running())
        f.write_text("not json")
        self.assertIsNone(instance.Instance(self.state).read())


class ServeTests(unittest.TestCase):
    """Two real ``serve`` processes on one state folder."""

    def setUp(self):
        self.env = Env(config={"port": 0}, serve=False).start()   # the world only; serve is the subject
        self.addCleanup(self.env.stop)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=5)
            if p.stdout and not p.stdout.closed:
                p.stdout.close()

    def serve(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT.parent)}
        p = subprocess.Popen([sys.executable, "-m", "metabot_app.cli", "serve"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p

    def wait_record(self):
        f = self.env.state / "server.json"
        for _ in range(200):
            if f.is_file():
                try:
                    return json.loads(f.read_text())
                except ValueError:
                    pass
            time.sleep(0.05)
        self.fail("no server.json")

    def meta(self, port):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/meta?t={self.env.token}",
                                     headers={"Host": f"127.0.0.1:{port}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_two_serves_give_one_server(self):
        first = self.serve()
        rec = self.wait_record()
        self.assertEqual(rec["pid"], first.pid)
        self.assertGreater(rec["port"], 0)
        self.assertEqual(self.meta(rec["port"])[0], 200)
        second = self.serve()
        out, _ = second.communicate(timeout=20)
        self.assertEqual(second.returncode, 0, out)
        self.assertIn(f"already running on 127.0.0.1:{rec['port']}", out)
        self.assertIsNone(first.poll())                    # the first one is untouched
        self.assertEqual(json.loads((self.env.state / "server.json").read_text())["pid"], first.pid)
        first.send_signal(signal.SIGTERM)                  # a clean stop releases the record
        first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0)
        self.assertFalse((self.env.state / "server.json").exists())

    def test_a_port_taken_by_another_program_is_left_alone(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        taken = blocker.getsockname()[1]
        cfg = json.loads(self.env.config.read_text())
        cfg["port"] = taken
        self.env.config.write_text(json.dumps(cfg))
        p = self.serve()
        rec = self.wait_record()
        self.assertNotEqual(rec["port"], taken)
        self.assertEqual(self.meta(rec["port"])[0], 200)
        out_line = ""
        p.send_signal(signal.SIGTERM)
        out_line = p.communicate(timeout=10)[0]
        self.assertIn(f"port {taken} is taken", out_line)


if __name__ == "__main__":
    unittest.main()
