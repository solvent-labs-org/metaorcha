"""21.4 hardening and the server-concurrency review findings: /api/meta build +
display.unit, the SSE hello and ping, the shared desks() snapshot, /ws/term caps
and idle close, log rotation, the keep-alive drain on a refused POST, the alias
lock, the process-group kill on a hung read."""
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server, ws_read_close, ws_read_frame  # noqa: E402
from metabot_app import mb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "src" / "metabot_app"


def cframe(opcode, payload=b""):
    """One masked client frame (payload < 126 bytes)."""
    mask = b"\x11\x22\x33\x44"
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + body


def open_stream(env):
    s = socket.create_connection(("127.0.0.1", env.port), timeout=10)
    s.sendall(f"GET /api/stream?t={env.token} HTTP/1.1\r\nHost: {env.host}\r\n\r\n".encode())
    return s


def read_until(s, marker, timeout=5):
    s.settimeout(timeout)
    data = b""
    while marker not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def proc_alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


class MetaStreamTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_meta_build_and_display_unit(self):
        self.assertEqual(server.BUILD, mb.build_id(server.HERE))   # configure() reads the tree that serves
        s, j = self.env.get("/api/meta")
        self.assertEqual(s, 200)
        self.assertEqual(j["app"], "MetaBot")
        self.assertEqual(j["build"], server.BUILD)
        # what a git checkout reports, from a checkout of its own: this tree may be a
        # `git archive` export, which has no .git and reads "dev"
        repo, _, sha = self.env.git_checkout()
        saved = server.BUILD
        server.BUILD = mb.build_id(repo)
        try:
            s, j = self.env.get("/api/meta")
        finally:
            server.BUILD = saved
        self.assertEqual(j["build"], sha[:7])
        self.assertRegex(j["build"], r"^[0-9a-f]{7}$")
        self.assertEqual(set(j["display"]), {"vnc", "unit"})
        self.assertIsNone(j["display"]["unit"])
        calls = [c for c in self.env.calls("systemctl")]
        self.assertEqual(calls[-1], ["--user", "is-active", "metachat-gui", "fc-gui"])   # a read, never an action
        # the unit that is active, cached 5 s
        os.environ["FAKE_ACTIVE_UNITS"] = "fc-gui"
        try:
            s, j = self.env.get("/api/meta")
            self.assertIsNone(j["display"]["unit"])       # still the cached answer
            server.MB._unit = [0.0, None]
            s, j = self.env.get("/api/meta")
            self.assertEqual(j["display"]["unit"], "fc-gui")
            os.environ["FAKE_ACTIVE_UNITS"] = "fc-gui,metachat-gui"
            server.MB._unit = [0.0, None]
            s, j = self.env.get("/api/meta")
            self.assertEqual(j["display"]["unit"], "metachat-gui")   # the new unit wins when both run
        finally:
            os.environ.pop("FAKE_ACTIVE_UNITS", None)
            server.MB._unit = [0.0, None]

    def test_build_id_reads_git_or_build_file_or_dev(self):
        import tempfile
        # a git checkout of its own, never this tree (a `git archive` export has no .git)
        repo, wt, sha = self.env.git_checkout()
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(mb.build_id(repo), sha[:7])        # .git is a folder; the ref is a loose file
        self.assertEqual(mb.build_id(wt), sha[:7])          # a worktree: .git is a "gitdir:" file
        ref = self.env.git("symbolic-ref", "HEAD", cwd=repo)
        self.env.git("pack-refs", "--all", cwd=repo)
        self.assertFalse((repo / ".git" / ref).exists())     # packed now: read from packed-refs
        self.assertEqual(mb.build_id(repo), sha[:7])
        self.assertEqual(mb.build_id(wt), sha[:7])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mb.build_id(d), "dev")
            (Path(d) / "BUILD").write_text("abc1234\n")
            self.assertEqual(mb.build_id(d), "abc1234")
            (Path(d) / "BUILD").write_text("not a build id; really\n")
            self.assertEqual(mb.build_id(d), "dev")
            # a plain .git dir with a ref
            g = Path(d) / ".git"
            (g / "refs" / "heads").mkdir(parents=True)
            (g / "HEAD").write_text("ref: refs/heads/main\n")
            (g / "refs" / "heads" / "main").write_text("0123456789abcdef0123456789abcdef01234567\n")
            self.assertEqual(mb.build_id(d), "0123456")

    def test_build_id_commit_needs_no_machine_identity(self):
        """A runner and a new machine have no git name or email. The fixture
        commit still has to exist, and the empty global file is not written."""
        blank = self.env.root / "empty-gitconfig"
        blank.write_text("")
        self.env._git_config_global = str(blank)
        repo, wt, sha = self.env.git_checkout()
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(mb.build_id(repo), sha[:7])
        self.assertEqual(mb.build_id(wt), sha[:7])
        self.assertEqual(blank.read_text(), "")

    def test_stream_hello_then_pings(self):
        saved = server.SSE_PING_S
        server.SSE_PING_S = 0.3
        s = open_stream(self.env)
        try:
            data = read_until(s, b"\nevent: hello\ndata: ")
            self.assertIn(b"200 OK", data)
            self.assertIn(b": connected\n\n", data)
            s.settimeout(5)
            while b"\n\n" not in data.split(b"event: hello\ndata: ", 1)[1]:
                data += s.recv(4096)
            hello = json.loads(data.split(b"event: hello\ndata: ", 1)[1].split(b"\n\n", 1)[0])
            self.assertEqual(hello, {"app": "MetaBot", "build": server.BUILD, "version": "0.1.0"})
            s.settimeout(2)
            got = b""
            end = time.time() + 1.2
            while time.time() < end:
                try:
                    got += s.recv(4096)
                except socket.timeout:
                    break
            self.assertGreaterEqual(got.count(b": ping\n\n"), 2)
            self.assertNotIn(b": ka", got)
        finally:
            server.SSE_PING_S = saved
            s.close()

    def test_stream_handler_notices_a_closed_client_and_pumps_are_shared(self):
        # server-concurrency-1: no per-client reader threads; a gone client is gone within ~1 s
        before = len(server._local_subs)
        socks = []
        for _ in range(3):
            s = open_stream(self.env)
            read_until(s, b"event: hello")
            socks.append(s)
        self.assertEqual(len(server._local_subs), before + 3)
        for s in socks:
            s.close()
        end = time.time() + 3
        while time.time() < end and len(server._local_subs) > before:
            time.sleep(0.05)
        self.assertEqual(len(server._local_subs), before)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_twelve_concurrent_desks_reads_share_one_tmux_scan(self):
        for _ in range(3):
            s, j = self.env.post("/api/spawn", {"kind": "shell"})
            self.assertEqual(s, 200)
        server.invalidate()
        self.env.clear_calls()
        results = []
        lock = threading.Lock()

        def one():
            s, j = self.env.get("/api/desks")
            with lock:
                results.append((s, sorted(d["name"] for d in j["desks"])))
        threads = [threading.Thread(target=one) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([s for s, _ in results], [200] * 12)
        self.assertEqual(len({tuple(n) for _, n in results}), 1)
        lists = [c for c in self.env.calls("tmux") if c[:1] == ["list-panes"]]
        self.assertLessEqual(len(lists), 2, lists)
        caps = [c for c in self.env.calls("tmux") if c[:1] == ["capture-pane"]]
        self.assertLessEqual(len(caps), 2 * 3)
        # a second burst after the TTL reads tmux again, once
        time.sleep(server.SNAP_TTL + 0.1)
        self.env.clear_calls()
        self.env.get("/api/desks")
        self.env.get("/api/desks")
        self.assertEqual(len([c for c in self.env.calls("tmux") if c[:1] == ["list-panes"]]), 1)

    def test_send_and_kill_read_the_snapshot_not_tmux_again(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "snap-a"})
        self.assertEqual(s, 200)
        self.env.get("/api/desks")
        self.env.clear_calls()
        s, j = self.env.post("/api/send", {"text": "hi", "target": "snap-a", "from": "desk-z"})
        self.assertEqual((s, j["via"]), (200, "lane"))
        self.assertEqual([c for c in self.env.calls("tmux") if c[:1] == ["list-panes"]], [])
        # kill reads a fresh view (a desk armed a moment ago must still refuse)
        self.env.get("/api/desks")
        self.env.arm("snap-a")
        s, j = self.env.post("/api/kill", {"name": "snap-a", "confirm": True})
        self.assertEqual((s, j), (403, {"ok": False, "error": "armed"}))
        self.assertIn("fc-snap-a", self.env.tmux_sessions())

    def test_dead_pane_drops_its_cwd(self):
        # server-concurrency-9: _pane_cwd is the panes that exist now
        alpha = str(self.env.projects / "alpha")
        self.env.add_session("fc-stale", cwd=alpha)
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "stale").write_text("stale\n")
        server.invalidate()
        s, j = self.env.get("/api/desks")
        d = [x for x in j["desks"] if x["name"] == "stale"][0]
        self.assertEqual((d["pane"], d["cwd"], d["project"]), (True, alpha, "alpha"))
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-stale"]
        self.env.tmux_state.write_text(json.dumps(st))
        server.invalidate()
        s, j = self.env.get("/api/desks")
        d = [x for x in j["desks"] if x["name"] == "stale"][0]
        self.assertEqual((d["pane"], d["cwd"], d["project"]), (False, "", "other"))
        self.assertNotIn("stale", server._pane_cwd)
        # a registered desk whose pane died keeps its project from the registry
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "reg-a", "cwd": alpha})
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-reg-a"]
        self.env.tmux_state.write_text(json.dumps(st))
        server.invalidate()
        s, j = self.env.get("/api/desks?scope=metachat")
        d = [x for x in j["desks"] if x["name"] == "reg-a"][0]
        self.assertEqual((d["pane"], d["cwd"], d["project"]), (False, "", "alpha"))


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_refused_post_leaves_the_connection_usable(self):
        # server-concurrency-2: the unread body of a 403 must not become the next request line
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.env.port, timeout=10)
        body = json.dumps({"text": "hello", "target": "claude"}).encode()
        for headers in ({"Content-Type": "application/json"},                       # no X-MetaChat -> csrf
                        {"Content-Type": "application/json", "X-MetaChat": "1", "Host": "evil:1"}):
            c.request("POST", f"/api/send?t={self.env.token}", body=body, headers=headers)
            r = c.getresponse()
            payload = json.loads(r.read())
            self.assertEqual((r.status, payload["ok"]), (403, False))
            sock = c.sock
            self.assertIsNotNone(sock)
            c.request("GET", f"/api/sessions?t={self.env.token}", headers={"Host": self.env.host})
            r = c.getresponse()
            raw = r.read()
            self.assertEqual(r.status, 200, raw)
            self.assertIn("sessions", json.loads(raw))
            self.assertIs(c.sock, sock)   # the same TCP connection served both
        c.close()
        # a refused POST with an oversized body closes the connection instead
        c = http.client.HTTPConnection("127.0.0.1", self.env.port, timeout=10)
        c.request("POST", f"/api/send?t={self.env.token}", body=b"x" * 70000,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        self.assertEqual(r.getheader("Connection"), "close")
        c.close()


class AliasRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_concurrent_renames_keep_every_alias(self):
        # server-concurrency-4: a lock and a unique temp file per write
        errors, lock = [], threading.Lock()

        def worker(i):
            try:
                for n in range(40):
                    server.set_alias(f"desk{i}", f"alias {i} {n}")
            except Exception as exc:   # noqa: BLE001
                with lock:
                    errors.append(repr(exc))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        a = server.load_aliases()
        self.assertEqual(sorted(a), [f"desk{i}" for i in range(6)])
        self.assertEqual(a["desk3"], "alias 3 39")
        self.assertEqual([p.name for p in self.env.state.glob("aliases.*.tmp")], [])
        # the route under the same load: every request answers
        codes = []

        def post(i):
            s, j = self.env.post("/api/alias", {"name": f"r{i}", "alias": f"n{i}"})
            with lock:
                codes.append(s)
        threads = [threading.Thread(target=post, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(codes, [200] * 12)
        s, j = self.env.get("/api/aliases")
        self.assertEqual(len(j["aliases"]), 18)

    def test_unwritable_state_is_502_not_a_dropped_connection(self):
        saved = server.ALIASES
        server.ALIASES = Path("/nonexistent-dir-for-metachat-test/aliases.json")
        try:
            s, j = self.env.post("/api/alias", {"name": "x", "alias": "y"})
        finally:
            server.ALIASES = saved
        self.assertEqual(s, 502)
        self.assertTrue(j["error"].startswith("cannot write aliases"))


class TermTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.saved = (server.TERM_IDLE_S, server.TERM_PING_S, server.TERM_MAX_PER_DESK)

    def tearDown(self):
        server.TERM_IDLE_S, server.TERM_PING_S, server.TERM_MAX_PER_DESK = self.saved
        self.env.stop()

    def open_term(self, desk):
        s, status, rest = self.env.ws(f"/ws/term?desk={desk}", origin=f"http://{self.env.host}")
        self.assertEqual(status, 101)
        return s, rest

    def test_fifth_terminal_on_a_desk_closes_4008(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "cap1"})
        self.assertEqual(s, 200)
        socks = []
        try:
            for _ in range(4):
                s, rest = self.open_term("cap1")
                op, payload, rest = ws_read_frame(s, rest)
                self.assertEqual(op, 2)
                socks.append(s)
            with server.TERM_LOCK:
                self.assertEqual(len(server.TERM_SOCKS["cap1"]), 4)
                self.assertTrue(all(isinstance(lk, type(threading.Lock())) for lk in server.TERM_SOCKS["cap1"].values()))
            s5, rest = self.open_term("cap1")
            code, reason = ws_read_close(s5, rest)
            self.assertEqual((code, reason), (4008, "too many terminals"))
            s5.close()
            with server.TERM_LOCK:
                self.assertEqual(len(server.TERM_SOCKS["cap1"]), 4)
            # closing one frees a slot
            socks[0].sendall(cframe(0x8, struct.pack(">H", 1000)))
            socks.pop(0).close()
            end = time.time() + 3
            while time.time() < end:
                with server.TERM_LOCK:
                    if len(server.TERM_SOCKS["cap1"]) == 3:
                        break
                time.sleep(0.05)
            s6, rest = self.open_term("cap1")
            op, payload, rest = ws_read_frame(s6, rest)
            self.assertEqual(op, 2)
            socks.append(s6)
            attaches = [c for c in self.env.calls("tmux") if c[:1] == ["attach-session"]]
            self.assertEqual(len(attaches), 5)   # the refused socket never attached
        finally:
            for s in socks:
                s.close()
        self.assertIn("refuse desk=cap1: too many terminals", (self.env.state / "ws.log").read_text())

    def test_idle_terminal_closes_4000(self):
        server.TERM_IDLE_S = 0.6
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "idle1"})
        s, rest = self.open_term("idle1")
        t0 = time.time()
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (4000, "idle"))
        self.assertLess(time.time() - t0, 3)
        s.close()
        time.sleep(0.2)
        self.assertIn("code=4000 idle", (self.env.state / "ws.log").read_text())
        with server.TERM_LOCK:
            self.assertEqual(len(server.TERM_SOCKS.get("idle1", {})), 0)

    def test_input_keeps_a_terminal_alive_and_two_missed_pongs_close_1001(self):
        server.TERM_IDLE_S = 0.8
        server.TERM_PING_S = 0.2
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "ping1"})
        s, rest = self.open_term("ping1")
        op, payload, rest = ws_read_frame(s, rest)
        self.assertEqual(op, 2)
        # answer every ping and keep typing: neither close fires
        end = time.time() + 1.3
        pings = 0
        while time.time() < end:
            try:
                op, payload, rest = ws_read_frame(s, rest, timeout=0.5)
            except socket.timeout:
                continue
            self.assertNotEqual(op, 8, payload)
            if op == 9:
                pings += 1
                s.sendall(cframe(0xA, payload))
                s.sendall(cframe(0x2, b" "))   # a keystroke resets the idle clock
        self.assertGreaterEqual(pings, 3)
        # now stay silent: pings go unanswered -> 1001 before the idle close
        server.TERM_IDLE_S = 60
        t0 = time.time()
        code, reason = ws_read_close(s, rest)
        self.assertEqual((code, reason), (1001, "peer gone"))
        self.assertLess(time.time() - t0, 3)
        s.close()

    def test_term_tmux_calls_carry_a_timeout_and_keepalive_is_set(self):
        # server-concurrency-7 / -5: the handler never parks on a wedged tmux server;
        # a half-open peer errors at the kernel's keepalive, not after 15 min
        src = (ROOT / "server.py").read_text()
        body = src.split("def term_session(")[1].split("\ndef vnc_bridge(")[0]
        for call in ('tmuxcmd.argv("new-session", "-d", "-s", session)', 'tmuxcmd.argv("set-option", "-t", session, *opt)'):
            self.assertIn(call, body)
        self.assertEqual(body.count("timeout=10"), 2)
        self.assertIn("1011", body)
        upgrade = src.split("def _upgrade(")[1].split("\n    def ")[0]
        self.assertIn("SO_KEEPALIVE", upgrade)
        self.assertIn("TCP_KEEPIDLE", upgrade)


class OrphanTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_cli_read_timeout_kills_the_grandchild(self):
        # server-concurrency-6: the read's process group dies with it
        pidfile = self.env.root / "orphan.pid"
        os.environ["FAKE_ORPHAN_PID_FILE"] = str(pidfile)
        server.MB.read_timeout = 1
        try:
            t0 = time.time()
            s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}})
        finally:
            os.environ.pop("FAKE_ORPHAN_PID_FILE", None)
            server.MB.read_timeout = 60
        self.assertEqual((s, j), (502, {"ok": False, "error": "timeout"}))
        self.assertLess(time.time() - t0, 8)
        pid = int(pidfile.read_text())
        end = time.time() + 2
        while time.time() < end and proc_alive(pid):
            time.sleep(0.05)
        self.assertFalse(proc_alive(pid), f"sleep {pid} outlived the timed-out read")
        src = (ROOT / "mb.py").read_text()
        # two sites may start a session of their own: run_captured (so a timed-out read's
        # group dies with it) and spawn_run_process (a run the app holds without tmux, ended
        # by one killpg); every other subprocess goes through run_captured
        self.assertEqual(src.count("start_new_session=True"), 2)
        for fn in ("def run_captured(", "def spawn_run_process("):
            self.assertIn("start_new_session=True", src.split(fn)[1].split("\ndef ")[0], fn)
        for fn in ("def cli_read", "def mbq_once(", "def add_account", "def _orcha_verify"):
            body = src.split(fn)[1].split("\ndef ")[0]
            self.assertIn("run_captured(", body, fn)
            self.assertNotIn("subprocess.run(", body, fn)
        # the warm reader kills its whole group on a timeout too
        wsrc = (ROOT / "mbqw.py").read_text()
        self.assertIn("start_new_session=True", wsrc)
        self.assertIn("killpg", wsrc)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_ws_log_and_server_log_rotate_at_2mb(self):
        wslog = self.env.state / "ws.log"
        wslog.write_bytes(b"x" * (server.LOG_LIMIT + 1))
        server.wslog("after rotation")
        self.assertTrue((self.env.state / "ws.log.1").is_file())
        self.assertEqual((self.env.state / "ws.log.1").stat().st_size, server.LOG_LIMIT + 1)
        self.assertLess(wslog.stat().st_size, 100)
        self.assertIn("after rotation", wslog.read_text())
        server.wslog("second line")   # under the limit: appended, the .1 copy stays
        self.assertEqual(wslog.read_text().count("\n"), 2)
        slog = self.env.state / "server.log"
        slog.write_bytes(b"y" * (server.LOG_LIMIT + 1))
        server.slog("rotated too")
        self.assertTrue((self.env.state / "server.log.1").is_file())
        self.assertIn("rotated too", slog.read_text())

    def test_local_events_file_keeps_its_last_300_lines(self):
        log = self.env.state / "local-events.jsonl"
        big = json.dumps({"via": "desk", "time": time.time(), "title": "metachat", "message": "m" * 6000,
                          "target": "a", "from": "b", "typed": False})
        with open(log, "w") as f:
            for i in range(400):
                f.write(big.replace('"m' + 'm' * 5999, f'"{i:04d}' + "m" * 5995) + "\n")
        self.assertGreater(log.stat().st_size, server.LOG_LIMIT)
        server.local_event("newest", "a", "b", via="desk", typed=False)
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), server.LOCAL_KEEP + 1)
        self.assertEqual(json.loads(lines[-1])["message"], "newest")
        self.assertTrue(json.loads(lines[0])["message"].startswith("0100"))   # lines 100..399 kept
        self.assertFalse((self.env.state / "local-events.jsonl.1").exists())
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        server.configure()   # a restart reads the bounded file
        self.assertEqual(len(server._local), server.LOCAL_KEEP)


if __name__ == "__main__":
    unittest.main()


class NagleTests(unittest.TestCase):
    """Speed plan item 5 (measured 11-09): with Nagle on, a small reply on a
    reused connection waits for the delayed ACK (about 40 ms)."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_nagle_is_off(self):
        self.assertIs(server.Handler.disable_nagle_algorithm, True)

    def test_a_second_request_on_one_connection_is_fast(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.env.port, timeout=10)
        best = 1.0
        try:
            for _ in range(6):
                t0 = time.time()
                c.request("GET", "/api/meta?t=" + self.env.token)
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 200)
                best = min(best, time.time() - t0) if _ else best
        finally:
            c.close()
        self.assertLess(best, 0.030, f"best reused-connection reply {best * 1000:.0f} ms")
