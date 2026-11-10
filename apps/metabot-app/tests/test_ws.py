"""4.19: /ws/term creates only the shared desk or registered desks; 4004 and 4001 codes."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
import json  # noqa: E402

from harness import Env, server, ws_read_close, ws_read_frame  # noqa: E402


class WsTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_unknown_desk_closes_4004_without_spawn(self):
        s, status, rest = self.env.ws("/ws/term?desk=ghost")
        self.assertEqual(status, 101)
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (4004, "no such desk"))
        s.close()
        news = [c for c in self.env.calls("tmux") if c[:1] in (["new-session"], ["attach-session"])]
        self.assertEqual(news, [])
        self.assertNotIn("fc-ghost", self.env.tmux_sessions())

    def test_foreign_origin_403(self):
        s, status, _ = self.env.ws("/ws/term", origin="http://evil.example:1")
        self.assertEqual(status, 403)
        s.close()

    def test_registered_desk_attaches_and_kill_closes_4001(self):
        st, j = self.env.post("/api/spawn", {"kind": "shell", "label": "ws1"})
        self.assertEqual(st, 200)
        s, status, rest = self.env.ws("/ws/term?desk=ws1", origin=f"http://{self.env.host}")
        self.assertEqual(status, 101)
        op, payload, rest = ws_read_frame(s, rest)
        self.assertEqual(op, 2)
        self.assertIn(b"attached fc-ws1", payload)
        st, j = self.env.post("/api/kill", {"name": "ws1", "confirm": True})
        self.assertEqual(st, 200)
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (4001, "killed"))
        s.close()

    def test_legacy_session_attaches_without_create(self):
        self.env.add_session("fc-legacy")
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=legacy")
        self.assertEqual(status, 101)
        op, payload, _ = ws_read_frame(s, rest)
        self.assertIn(b"attached fc-legacy", payload)
        s.close()
        time.sleep(0.3)
        calls = [c[0] for c in self.env.calls("tmux")]
        self.assertNotIn("new-session", calls)
        self.assertIn("attach-session", calls)

    def test_vnc_backend_down_closes(self):
        # ops/F7: the harness points VNC_BACKEND at a closed port of its own,
        # so this never touches a real x11vnc on 127.0.0.1:5900
        self.assertNotEqual(server.VNC_BACKEND[1], 5900)
        s, status, rest = self.env.ws("/ws/vnc", origin=f"http://{self.env.host}")
        self.assertEqual(status, 101)
        op, payload, _ = ws_read_frame(s, rest)
        self.assertEqual(op, 8)   # close: backend down
        s.close()
        s, j = self.env.get("/api/meta")
        self.assertIs(j["display"]["vnc"], False)

    def test_vnc_bridge_reaches_the_injected_backend(self):
        import socket
        import threading
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        got = []

        def backend():
            # the first connection is /api/meta's probe (it closes at once);
            # the bridge's connection sends bytes back
            srv.settimeout(10)
            for _ in range(4):
                c, _ = srv.accept()
                c.settimeout(5)
                c.sendall(b"RFB 003.008\n")
                try:
                    data = c.recv(64)
                except OSError:
                    data = b""
                c.close()
                if data:
                    got.append(data)
                    return
        th = threading.Thread(target=backend, daemon=True)
        th.start()
        saved = server.VNC_BACKEND
        server.VNC_BACKEND = ("127.0.0.1", srv.getsockname()[1])
        try:
            s, j = self.env.get("/api/meta")
            self.assertIs(j["display"]["vnc"], True)
            s, status, rest = self.env.ws("/ws/vnc", origin=f"http://{self.env.host}")
            self.assertEqual(status, 101)
            op, payload, rest = ws_read_frame(s, rest)
            self.assertEqual((op, payload), (2, b"RFB 003.008\n"))
            s.sendall(bytes([0x82, 0x80 | 3]) + b"\x00\x00\x00\x00" + b"RFB")
            th.join(5)
            self.assertEqual(got, [b"RFB"])
            s.close()
        finally:
            server.VNC_BACKEND = saved
            srv.close()

    def test_dead_registered_desk_is_respawned_from_its_registry_entry(self):
        # SEC-4: never a bare new-session for a registered desk
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": "/acc/work", "max_at_once": 3}]))
        st, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work", "label": "acc1",
                                             "cwd": str(self.env.projects / "beta")})
        self.assertEqual(st, 200, j)
        first = [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]][-1]
        st_file = json.loads(self.env.tmux_state.read_text())
        del st_file["sessions"]["fc-acc1"]
        self.env.tmux_state.write_text(json.dumps(st_file))
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=acc1", origin=f"http://{self.env.host}")
        self.assertEqual(status, 101)
        op, payload, rest = ws_read_frame(s, rest)
        self.assertEqual(op, 2)
        self.assertIn(b"attached fc-acc1", payload)
        s.close()
        time.sleep(0.3)
        news = [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]
        self.assertEqual(news, [first])   # the same argv: cwd, METABOT_HOME, account env, -u list, script
        self.assertIn("-e", first)
        self.assertIn("CLAUDE_CONFIG_DIR=/acc/work", first)
        self.assertEqual(first[5], str(self.env.projects / "beta"))
        self.assertIn("fc-acc1", self.env.tmux_sessions())
        s, j = self.env.get("/api/events?limit=1")
        self.assertEqual((j["events"][0]["kind"], j["events"][0]["desk"]), ("spawn", "acc1"))
        self.assertTrue(j["events"][0]["data"].get("respawn"))

    def test_dead_run_desk_closes_4004_without_a_new_session(self):
        # a run's prompt file is gone with the desk: it cannot be re-created
        st, j = self.env.post("/api/run", {"task": "x", "engine": "claude",
                                           "cwd": str(self.env.projects / "alpha")})
        self.assertEqual(st, 200, j)
        st_file = json.loads(self.env.tmux_state.read_text())
        del st_file["sessions"]["fc-run-1"]
        self.env.tmux_state.write_text(json.dumps(st_file))
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=run-1")
        self.assertEqual(status, 101)
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (4004, "no such desk"))
        s.close()
        news = [c for c in self.env.calls("tmux") if c[:1] in (["new-session"], ["attach-session"])]
        self.assertEqual(news, [])

    def test_dead_desk_with_a_vanished_account_closes_4004(self):
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": "/acc/work", "max_at_once": 3}]))
        st, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work", "label": "acc2"})
        self.assertEqual(st, 200, j)
        (self.env.mbhome / "fake-logins.json").write_text("[]")
        st_file = json.loads(self.env.tmux_state.read_text())
        del st_file["sessions"]["fc-acc2"]
        self.env.tmux_state.write_text(json.dumps(st_file))
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=acc2")
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (4004, "no such desk"))
        s.close()
        self.assertEqual([c for c in self.env.calls("tmux") if c[:1] == ["new-session"]], [])

    def test_the_shared_desk_is_created_on_demand(self):
        """B3: with the default config the shared desk is "main"; nothing the
        terminal or a spawn makes, and nothing in the tree, says "floor"."""
        self.assertEqual(server.DEFAULTS["shared_desk"], "main")
        s, status, rest = self.env.ws("/ws/term")
        self.assertEqual(status, 101)
        op, payload, _ = ws_read_frame(s, rest)
        self.assertIn(b"attached main", payload)
        s.close()
        self.assertIn("main", self.env.tmux_sessions())
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "s-one"})
        self.assertEqual(s, 200, j)
        s, j = self.env.post("/api/spawn", {"kind": "claude"})
        self.assertEqual(s, 200, j)
        server.invalidate()
        s, tree = self.env.get("/api/tree")
        self.assertEqual(s, 200)
        made = [c for c in self.env.calls("tmux") if c[:1] in (["new-session"], ["attach-session"], ["set-option"])]
        self.assertTrue(made)
        self.assertNotIn("floor", json.dumps(made).lower())
        self.assertNotIn("floor", json.dumps(tree).lower())
        self.assertNotIn("floor", json.dumps(self.env.tmux_sessions()).lower())
        src = (Path(server.__file__)).read_text()
        self.assertNotIn('"floor"', src)
        self.assertNotIn("'floor'", src)

    def test_the_shared_desk_name_comes_from_config(self):
        cfg = json.loads(self.env.config.read_text())
        cfg["shared_desk"] = "lobby"
        self.env.config.write_text(json.dumps(cfg))
        server.configure()
        s, status, rest = self.env.ws("/ws/term")
        self.assertEqual(status, 101)
        op, payload, _ = ws_read_frame(s, rest)
        self.assertIn(b"attached lobby", payload)
        s.close()
        self.assertIn("lobby", self.env.tmux_sessions())
        for bad in ("fc-x", "a b", "", "x" * 41):
            cfg["shared_desk"] = bad
            self.env.config.write_text(json.dumps(cfg))
            server.configure()
            self.assertEqual(server.SHARED_DESK, "main", bad)


if __name__ == "__main__":
    unittest.main()
