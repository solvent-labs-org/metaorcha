"""Section 13: the evening hotfixes phase 1 keeps — deliver() with from / kind,
local events in /api/history and /api/stream, aliases, bin/mc-send."""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, ROOT, server  # noqa: E402

MC_SEND = ROOT.parents[1] / "bin" / "mc-send"


def pane_desk(env, name, cmd="cursor-agent"):
    """A desk whose pane tree carries a CLI command line (a renamed sleep)."""
    proc = subprocess.Popen(["bash", "-c", f"exec -a {cmd} sleep 30"])
    time.sleep(0.2)
    env.add_session(f"fc-{name}", pid=proc.pid)
    server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
    (server.NAMES_DIR / name).write_text(name + "\n")
    server._sess_cache[0] = 0.0
    return proc


def idle_desk(env, name):
    env.add_session(f"fc-{name}")
    server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
    (server.NAMES_DIR / name).write_text(name + "\n")
    server._sess_cache[0] = 0.0


def open_stream(env):
    s = socket.create_connection(("127.0.0.1", env.port), timeout=10)
    s.sendall(f"GET /api/stream?t={env.token} HTTP/1.1\r\nHost: {env.host}\r\n\r\n".encode())
    data = b""
    while b": connected\n\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    return s


def read_event(s):
    """The next data: event on an open SSE socket (keepalive comments skipped)."""
    buf = b""
    s.settimeout(10)
    while True:
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            if block.startswith(b"data: "):
                return json.loads(block[len(b"data: "):])
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("stream closed")
        buf += chunk


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_empty_text_and_self_target_400(self):
        s, j = self.env.post("/api/send", {"text": "   ", "target": "x"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "empty"}))
        s, j = self.env.post("/api/send", {"text": "hi", "target": "desk-z", "from": "desk-z"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "that is this desk"}))

    def test_tablet_target_from_a_desk_lands_in_its_lane(self):
        s, j = self.env.post("/api/send", {"text": "done", "target": "tablet", "from": "worker-1"})
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "lane", "reach": "tablet"}))
        s, j = self.env.get("/api/history")
        ev = j["messages"][-1]
        self.assertEqual((ev["via"], ev["from"], ev["target"], ev["message"], ev["typed"]),
                         ("desk", "worker-1", "worker-1", "done", False))
        log = (self.env.state / "local-events.jsonl")
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])["message"], "done")
        # from the tablet (no from) "tablet" is not a desk: 409 as before
        s, j = self.env.post("/api/send", {"text": "done", "target": "tablet"})
        self.assertEqual((s, j["reach"]), (409, "none"))

    def test_desk_to_unreachable_desk_is_a_lane_record(self):
        idle_desk(self.env, "idle-two")
        s, j = self.env.post("/api/send", {"text": "ping", "target": "idle-two", "from": "worker-1"})
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "lane", "reach": "idle"}))
        s, j = self.env.post("/api/send", {"text": "ping", "target": "ghost", "from": "worker-1"})
        self.assertEqual((s, j["via"], j["reach"]), (200, "lane", "none"))
        s, j = self.env.get("/api/history")
        lanes = [e for e in j["messages"] if e["via"] == "desk"]
        self.assertEqual([(e["from"], e["target"]) for e in lanes],
                         [("worker-1", "idle-two"), ("worker-1", "ghost")])
        # the tablet itself still gets 409 for both
        s, j = self.env.post("/api/send", {"text": "ping", "target": "idle-two"})
        self.assertEqual((s, j["reach"]), (409, "idle"))

    def test_desk_to_pane_desk_prefixes_and_records(self):
        proc = pane_desk(self.env, "pane-two")
        try:
            self.env.clear_calls()
            s, j = self.env.post("/api/send", {"text": "look at this", "target": "pane-two",
                                               "from": "worker-1"})
            self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "pane", "reach": "pane"}))
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertEqual(buf, '[from worker-1 via metachat] look at this\n(reply: mc-send worker-1 "...")')
            s, j = self.env.get("/api/history")
            ev = j["messages"][-1]
            self.assertEqual((ev["via"], ev["from"], ev["target"], ev["message"], ev["typed"]),
                             ("desk", "worker-1", "pane-two", "look at this", True))
            # an unlisted sender name is reduced to safe characters
            s, j = self.env.post("/api/send", {"text": "x", "target": "pane-two", "from": "we ird/name!"})
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertTrue(buf.startswith("[from weirdname via metachat] x"))
        finally:
            proc.kill()
            proc.wait()

    def test_handoff_template(self):
        proc = pane_desk(self.env, "pane-three", cmd="claude")
        try:
            s, j = self.env.post("/api/send", {"text": "finish the report", "target": "pane-three",
                                               "from": "worker-1", "kind": "handoff"})
            self.assertEqual((s, j["via"]), (200, "pane"))
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertEqual(buf, server.HANDOFF_TPL.format(frm="worker-1", reply="worker-1",
                                                            text="HANDOFF: finish the report"))
            self.assertTrue(buf.startswith("[HANDOFF from worker-1 via metachat]"))
            self.assertIn('mc-send worker-1 "took handoff: <one line>"', buf)
            # from the tablet the sender is named "the tablet", and the reply address
            # is the routable "tablet", never a desk called "the" (review server-flows-3)
            s, j = self.env.post("/api/send", {"text": "carry on", "target": "pane-three", "kind": "handoff"})
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertTrue(buf.startswith("[HANDOFF from the tablet via metachat]"))
            self.assertIn('mc-send tablet "took handoff: <one line>"', buf)
            self.assertNotIn("mc-send the tablet", buf)
            s, j = self.env.get("/api/history")
            ev = j["messages"][-1]
            self.assertEqual((ev["via"], ev["from"], ev["typed"]), ("in", "", True))
        finally:
            proc.kill()
            proc.wait()

    def test_tablet_pane_send_is_a_local_event_on_the_stream(self):
        proc = pane_desk(self.env, "pane-four")
        try:
            stream = open_stream(self.env)
            try:
                s, j = self.env.post("/api/send", {"text": "hello", "target": "pane-four"})
                self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "pane", "reach": "pane"}))
                ev = read_event(stream)
            finally:
                stream.close()
            self.assertEqual(set(ev), {"via", "time", "title", "message", "target", "from", "typed"})
            self.assertEqual((ev["via"], ev["target"], ev["from"], ev["message"], ev["typed"]),
                             ("in", "pane-four", "", "hello", True))
            s, j = self.env.get("/api/history")
            self.assertEqual(j["messages"][-1]["message"], "hello")
            self.assertTrue(j["messages"][-1]["typed"])
        finally:
            proc.kill()
            proc.wait()

    def test_local_events_survive_a_restart_and_age_out(self):
        self.env.post("/api/send", {"text": "kept", "target": "tablet", "from": "worker-1"})
        old = {"via": "desk", "time": time.time() - 90000, "title": "metachat", "message": "old",
               "target": "a", "from": "b", "typed": False}
        with open(self.env.state / "local-events.jsonl", "a") as f:
            f.write(json.dumps(old) + "\n" + "not json\n")
        server.configure()   # a restart re-reads the file
        s, j = self.env.get("/api/history")
        self.assertEqual([e["message"] for e in j["messages"]], ["kept"])


class AliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_aliases_roundtrip_cap_and_delete(self):
        s, j = self.env.get("/api/aliases")
        self.assertEqual((s, j), (200, {"aliases": {}}))
        s, j = self.env.post("/api/alias", {"name": "claude-2", "alias": " Reviewer\x07 " + "x" * 60})
        self.assertEqual((s, j), (200, {"ok": True, "error": ""}))
        s, j = self.env.get("/api/aliases")
        self.assertEqual(j["aliases"], {"claude-2": ("Reviewer " + "x" * 60)[:48]})
        f = self.env.state / "aliases.json"
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        s, j = self.env.post("/api/alias", {"name": "claude-2", "alias": ""})
        self.assertEqual(s, 200)
        s, j = self.env.get("/api/aliases")
        self.assertEqual(j["aliases"], {})
        s, j = self.env.post("/api/alias", {"name": "", "alias": "x"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "no name"}))
        s, j = self.env.post("/api/alias", {"name": 5, "alias": "x"})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/alias", {"name": "a", "alias": "x"}, headers={"X-MetaChat": None})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))


@unittest.skipUnless(MC_SEND.is_file(), "bin/mc-send missing")
class McSendTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def run_mc(self, *args, desk="worker-1", state=None):
        env = dict(os.environ, METACHAT_URL=f"http://127.0.0.1:{self.env.port}",
                   METACHAT_DESK=desk)
        env.pop("TMUX", None)
        if state is None:
            env["METACHAT_STATE"] = str(self.env.state)
        else:
            env.pop("METACHAT_STATE", None)
        return subprocess.run(["bash", str(MC_SEND), *args], capture_output=True, text=True,
                              timeout=30, env=env)

    def test_mc_send_to_a_pane_desk_and_to_the_tablet(self):
        proc = pane_desk(self.env, "pane-five")
        try:
            r = self.run_mc("pane-five", "hello", "from", "a", "desk")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to pane-five via pane"), r.stderr)
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertTrue(buf.startswith("[from worker-1 via metachat] hello from a desk"), buf)
            r = self.run_mc("tablet", "all done")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to tablet via lane"), r.stderr)
            s, j = self.env.get("/api/history")
            self.assertEqual([(e["via"], e["from"], e["target"]) for e in j["messages"]],
                             [("desk", "worker-1", "pane-five"), ("desk", "worker-1", "worker-1")])
            r = self.run_mc("--list")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("pane-five", r.stdout)
            self.assertIn("pane", r.stdout)
            r = self.run_mc("worker-1", "to myself")
            self.assertEqual(r.returncode, 1)
            self.assertIn("not sent", r.stdout)
            self.assertNotIn(self.env.token, r.stdout + r.stderr)
        finally:
            proc.kill()
            proc.wait()

    def test_mc_send_finds_the_token_without_metachat_state(self):
        # METACHAT_STATE unset: the token is the one in ~/.local/state/metachat
        state = self.env.home / ".local/state/metachat"
        state.mkdir(parents=True)
        (state / "token").write_text(self.env.token + "\n")
        r = self.run_mc("tablet", "via default state", state=False)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to tablet via lane"), r.stderr)
        self.assertTrue(MC_SEND.read_text().count("X-MetaChat: 1"))
        self.assertNotIn("floor" + "-chat", MC_SEND.read_text())
        self.assertNotIn("floor" + " inbox", MC_SEND.read_text())


if __name__ == "__main__":
    unittest.main()
