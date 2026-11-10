"""B1: the chat inbox seam. The app ships no inbox; ``NullInbox`` is the default and
shows nothing. An adapter file named by ``inbox_module`` adds one, and a broken one
is NullInbox plus one log line. The contract is exercised with the fixture adapter
``fixtures/adapters/echo_inbox.py``."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server  # noqa: E402
from metabot_app import inbox  # noqa: E402

ECHO = FIX / "adapters" / "echo_inbox.py"
IDLE_CLAUDE = "╭────────────╮\n│ >          │\n╰────────────╯\n  ? for shortcuts\n"


def log_lines(env, needle="inbox adapter"):
    try:
        return [x for x in (env.state / "server.log").read_text().splitlines() if needle in x]
    except OSError:
        return []


def open_stream(env):
    s = socket.create_connection(("127.0.0.1", env.port), timeout=10)
    s.sendall(f"GET /api/stream?t={env.token} HTTP/1.1\r\nHost: {env.host}\r\n\r\n".encode())
    data = b""
    while b"event: hello" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    return s


def read_events(s, timeout):
    out, buf = [], b""
    end = time.time() + timeout
    while time.time() < end:
        s.settimeout(max(0.05, end - time.time()))
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            if block.startswith(b"data: "):
                out.append(json.loads(block[6:]))
    return out


class NullInboxTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_null_is_the_default_and_shows_nothing(self):
        self.assertIsInstance(server.INBOX, inbox.NullInbox)
        s, meta = self.env.get("/api/meta")
        self.assertEqual(s, 200)
        self.assertIsNone(meta["inbox"])
        (server.NAMES_DIR).mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "gone-one").write_text("gone-one\n")   # a name with no desk here
        server.invalidate()
        s, tree = self.env.get("/api/tree")
        self.assertEqual(s, 200)
        self.assertEqual(set(tree), {"waiting", "projects", "other", "ended"})
        self.assertEqual([d["name"] for d in tree["ended"]], ["gone-one"])
        s, desks = self.env.get("/api/desks")
        self.assertEqual(desks["streams"], {})
        for body in (meta, tree, desks):
            text = json.dumps(body).lower()
            self.assertNotIn("floor", text)
            self.assertNotIn("ntfy", text)
        self.assertEqual(log_lines(self.env), [])

    def test_nothing_is_ever_made_under_the_claude_folder(self):
        """B2: the app's own session names live in <state>/names; with no inbox
        adapter, spawning, starting a CLI, sending, killing and resuming write
        nothing under HOME/.claude."""
        claude = self.env.home / ".claude"
        alpha = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/spawn", {"kind": "claude", "cwd": alpha, "label": "c-one"})
        self.assertEqual(s, 200, j)
        s, j = self.env.post("/api/spawn", {"kind": "shell", "cwd": alpha, "label": "s-one"})
        self.assertEqual(s, 200, j)
        s, j = self.env.post("/api/pane", {"target": "s-one", "action": "start-claude"})
        self.assertEqual(s, 200, j)
        self.env.post("/api/send", {"text": "hi", "target": "c-one"})
        self.env.post("/api/pane", {"target": "c-one", "action": "resume"})
        s, j = self.env.post("/api/kill", {"name": "c-one", "confirm": True})
        self.assertEqual(s, 200, j)
        for path in ("/api/desks", "/api/tree", "/api/meta", "/api/history", "/api/sessions"):
            self.assertEqual(self.env.get(path)[0], 200, path)
        self.assertEqual(sorted(str(p.relative_to(claude)) for p in claude.rglob("*")), [])
        self.assertTrue(any((self.env.state / "names").iterdir()))   # the names went to the state folder

    def test_arm_and_a_send_with_no_desk_say_there_is_no_inbox(self):
        s, j = self.env.post("/api/pane", {"target": "nobody", "action": "arm"})
        self.assertEqual(s, 409)
        s, j = self.env.post("/api/send", {"text": "hi", "target": ""})
        self.assertEqual((s, j["ok"], j["via"]), (409, False, ""), j)
        s, j = self.env.post("/api/send", {"text": "hi", "target": "nobody"})
        self.assertEqual((s, j["ok"]), (409, False), j)
        self.assertEqual(j["error"], "no terminal desk here")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = Env({"inbox_module": str(ECHO)}).start()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.env.stop()

    def adapter(self):
        return server.INBOX.inner

    def claude_desk(self, name):
        proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 60"])
        self.procs.append(proc)
        time.sleep(0.2)
        self.env.add_session(f"fc-{name}", pid=proc.pid, pane=IDLE_CLAUDE)
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / name).write_text(name + "\n")
        server.invalidate()

    def test_meta_is_exposed_to_the_page(self):
        self.assertIsInstance(server.INBOX, inbox.Guarded)
        s, j = self.env.get("/api/meta")
        self.assertEqual(j["inbox"], {"name": "echo inbox", "arm_hint": "this desk's agent holds the echo inbox",
                                      "arm_command": "/echo-arm {desk}"})
        s, j = self.env.get("/api/desks")
        self.assertEqual(j["streams"], {"echo@local": True})
        self.assertEqual(log_lines(self.env), [])

    def test_deliver_goes_through_the_adapter_and_its_events_reach_the_page(self):
        self.adapter().set_armed(["desk-b"])
        server.invalidate()
        s, j = self.env.get("/api/desks")
        row = next(d for d in j["desks"] if d["name"] == "desk-b")
        self.assertEqual((row["armed"], row["reach"]), (True, "inbox"))
        stream = open_stream(self.env)
        try:
            self.assertEqual(self.adapter().starts, 1)   # started once, by the first page
            s, j = self.env.post("/api/send", {"text": "hello b", "target": "desk-b"})
            self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "inbox", "reach": "inbox"}))
            self.assertEqual(self.adapter().sent(), [{"desk": "desk-b", "text": "hello b"}])
            events = read_events(stream, 1.0)
            self.assertEqual([(e["via"], e["target"], e["message"]) for e in events],
                             [("in", "desk-b", "hello b")])
            second = open_stream(self.env)
            second.close()
            self.assertEqual(self.adapter().starts, 1)
        finally:
            stream.close()
        s, j = self.env.get("/api/history")
        self.assertIn(("in", "desk-b", "hello b"), [(e["via"], e["target"], e["message"]) for e in j["messages"]])
        # an armed desk is never killed
        self.env.add_session("fc-desk-b")
        s, j = self.env.post("/api/kill", {"name": "desk-b", "confirm": True})
        self.assertEqual((s, j), (403, {"ok": False, "error": "armed"}))

    def test_none_falls_through_to_the_pane(self):
        self.claude_desk("pane-one")
        s, j = self.env.post("/api/send", {"text": "typed please", "target": "pane-one"})
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "pane", "reach": "pane"}))
        self.assertEqual(self.adapter().sent(), [])
        self.assertEqual(self.env.tmux_loaded()[-1][1], b"typed please")
        # a desk writing to a desk nobody runs: the lane, still not the adapter
        s, j = self.env.post("/api/send", {"text": "note", "target": "nobody", "from": "pane-one"})
        self.assertEqual((s, j["via"]), (200, "lane"))
        self.assertEqual(self.adapter().sent(), [])

    def test_arm_goes_through_the_adapter(self):
        self.claude_desk("pane-one")
        s, j = self.env.post("/api/pane", {"target": "pane-one", "action": "arm"})
        self.assertEqual((s, j), (200, {"ok": True, "error": ""}))
        self.assertEqual(self.env.tmux_loaded()[-1][1], b"/echo-arm pane-one")
        server.invalidate()
        s, j = self.env.get("/api/desks")
        self.assertTrue(next(d for d in j["desks"] if d["name"] == "pane-one")["armed"])

    def test_names_only_rows_go_to_the_adapter_bucket(self):
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "far-one").write_text("far-one\n")
        server.invalidate()
        s, j = self.env.get("/api/tree")
        self.assertEqual(set(j), {"waiting", "projects", "other", "ended", "inbox"})
        self.assertEqual(j["inbox"]["label"], "Echo")
        self.assertEqual([d["name"] for d in j["inbox"]["desks"]], ["far-one"])
        self.assertEqual(j["ended"], [])


BROKEN = {
    "syntax": "def make_inbox(ctx)\n",
    "raises at import": "raise RuntimeError('no')\n",
    "no factory": "X = 1\n",
    "factory raises": "def make_inbox(ctx):\n    raise ValueError('no')\n",
    "missing methods": "class I:\n    def meta(self):\n        return {}\n\ndef make_inbox(ctx):\n    return I()\n",
}


class BrokenAdapterTests(unittest.TestCase):
    def check_null_with_one_line(self, path):
        with Env({"inbox_module": str(path)}) as env:
            self.assertIsInstance(server.INBOX, inbox.NullInbox)
            self.assertEqual(len(log_lines(env)), 1, log_lines(env))
            s, j = env.get("/api/meta")
            self.assertEqual((s, j["inbox"]), (200, None))
            s, j = env.get("/api/tree")
            self.assertEqual(s, 200)

    def test_each_broken_adapter_is_null_plus_one_log_line(self):
        with tempfile.TemporaryDirectory() as d:
            for what, src in BROKEN.items():
                with self.subTest(what):
                    p = Path(d) / (what.replace(" ", "_") + ".py")
                    p.write_text(src)
                    self.check_null_with_one_line(p)

    def test_a_missing_or_relative_path_is_null_plus_one_log_line(self):
        self.check_null_with_one_line("/nonexistent/inbox_adapter.py")
        self.check_null_with_one_line("relative/inbox_adapter.py")

    def test_a_method_that_raises_answers_as_null_and_logs_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "flaky.py"
            p.write_text(ECHO.read_text() + "\n\nclass Flaky(EchoInbox):\n    def armed(self):\n"
                         "        raise OSError('gone')\n\n\ndef make_inbox(ctx):\n    return Flaky(ctx)\n")
            with Env({"inbox_module": str(p)}) as env:
                for _ in range(3):
                    server.invalidate()
                    s, j = env.get("/api/desks")
                    self.assertEqual(s, 200)
                self.assertEqual(len(log_lines(env)), 1, log_lines(env))
                self.assertIn("armed failed", log_lines(env)[0])


if __name__ == "__main__":
    unittest.main()
