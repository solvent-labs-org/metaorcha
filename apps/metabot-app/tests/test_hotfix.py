"""Section 13: the evening hotfixes phase 1 keeps — deliver() with from / kind,
local events in /api/history and /api/stream, aliases, bin/mc-send,
metabot-app send."""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import ECHO_INBOX, Env, ROOT, server  # noqa: E402

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
            self.assertEqual(buf, '[from worker-1 via metachat] look at this\n'
                                  '(reply: metabot mail send worker-1 --from pane-two -m "...")')
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
            self.assertEqual(buf, server.HANDOFF_TPL.format(frm="worker-1", text="HANDOFF: finish the report",
                                                            reply_cmd=server.reply_cmd("pane-three", "worker-1")))
            self.assertTrue(buf.startswith("[HANDOFF from worker-1 via metachat]"))
            # 11-11 (found on the tablet): mc-send is a repo script, not in the wheel; with no
            # inbox adapter the reply hint names the core's mail, which every install has
            self.assertIn('metabot mail send worker-1 --from pane-three -m "took handoff: <one line>"', buf)
            self.assertNotIn("mc-send", buf)
            # from the tablet the sender is named "the tablet", and the reply address
            # is the routable "tablet", never a desk called "the" (review server-flows-3)
            s, j = self.env.post("/api/send", {"text": "carry on", "target": "pane-three", "kind": "handoff"})
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertTrue(buf.startswith("[HANDOFF from the tablet via metachat]"))
            self.assertIn('metabot mail send tablet --from pane-three -m "took handoff: <one line>"', buf)
            self.assertNotIn("the tablet -m", buf)
            s, j = self.env.get("/api/history")
            ev = j["messages"][-1]
            self.assertEqual((ev["via"], ev["from"], ev["typed"]), ("in", "", True))
        finally:
            proc.kill()
            proc.wait()

    def test_mail_to_the_tablet_lands_in_the_senders_lane(self):
        # 11-11: a desk that answers the person with `metabot mail send tablet ...` must be
        # heard: the app reads that mailbox once per mirror pass into the sender's lane
        box = self.env.mbhome / "mail" / "tablet"
        box.mkdir(parents=True)
        (box / "20261111T201658-cu-b-a12f2adc.md").write_text(
            "from: cu-b\nto: tablet\nat: 2026-11-11T20:16:58+05:00\n\ntook handoff: \x1b[31mpong\x07\n")
        (box / "notes.txt").write_text("not mail\n")
        server.mail_to_lane_once()
        s, j = self.env.get("/api/history")
        ev = j["messages"][-1]
        self.assertEqual((ev["message"], ev["target"], ev["from"], ev["via"], ev["typed"]),
                         ("took handoff: pong", "cu-b", "cu-b", "desk", False))
        self.assertEqual(sorted(p.name for p in box.iterdir()), ["notes.txt", "read"])
        self.assertTrue((box / "read" / "20261111T201658-cu-b-a12f2adc.md").exists())
        n = len(j["messages"])
        server.mail_to_lane_once()   # read once: a second pass adds nothing
        s, j = self.env.get("/api/history")
        self.assertEqual(len(j["messages"]), n)

    def test_a_free_text_prompt_is_a_text_ask_that_chat_answers(self):
        # 11-11 (found on the tablet): Cursor's "Tell the agent what to do instead" is a line
        # the person types into; the page must be able to answer it, so it is the `text` ask
        # and /api/send types the answer instead of refusing a waiting desk
        proc = pane_desk(self.env, "pane-four", cmd="cursor-agent")
        try:
            # the menu that offers the line is still a menu (found on the tablet 22:00: the
            # option text matched, the page offered a text line, the typed words plus Enter
            # chose "Run (once)" and ran the command the person had declined)
            self.env.set_pane("fc-pane-four", "\n".join(["old"] * 30) + "\n"
                              " Run this command?\n Not in allowlist: false\n  → Run (once) (y)\n"
                              "    Add Shell(false) to allowlist? (tab)\n    Run Everything (shift+tab)\n"
                              "    Skip & tell the agent what to do instead (esc or n)\n")
            server.MB._needs.clear()
            server.invalidate()
            s, j = self.env.get("/api/desks")
            row = next((d for d in j["desks"] if d["name"] == "pane-four"), None)
            self.assertEqual((row["needs_you"], row["ask"]), (True, "select"))
            s, j = self.env.post("/api/send", {"text": "say done instead", "target": "pane-four"})
            self.assertEqual((s, j["ok"]), (409, False), j)
            self.env.set_pane("fc-pane-four", "\n".join(["old"] * 30) + "\n"
                              "  → Tell the agent what to do instead (Enter to send, empty to skip, Esc to cancel)\n")
            server.MB._needs.clear()
            server.invalidate()
            s, j = self.env.get("/api/desks")
            row = next((d for d in j["desks"] if d["name"] == "pane-four"), None)
            self.assertIsNotNone(row, [d["name"] for d in j["desks"]])
            self.assertEqual((row["needs_you"], row["ask"]), (True, "text"))
            s, j = self.env.post("/api/send", {"text": "use metabot mail instead", "target": "pane-four"})
            self.assertEqual((s, j["ok"], j["via"]), (200, True, "pane"), j)
            self.assertEqual(self.env.tmux_loaded()[-1][1].decode(), "use metabot mail instead")
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


class SendSubcommandTests(unittest.TestCase):
    """11-12 (D2): the wheel ships the desk's reply command as `metabot-app send`
    (bin/mc-send is a repo script and never leaves it). The subcommand reads
    METACHAT_URL / METACHAT_STATE from the desk env that spawn_argv adds, and
    names the sender from its METABOT_MAILBOX."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def run_send(self, *args, mailbox="worker-2"):
        env = dict(os.environ, METACHAT_URL=f"http://127.0.0.1:{self.env.port}",
                   METACHAT_STATE=str(self.env.state), PYTHONPATH=str(ROOT.parent))
        if mailbox is None:
            env.pop("METABOT_MAILBOX", None)
        else:
            env["METABOT_MAILBOX"] = mailbox
        return subprocess.run([sys.executable, "-m", "metabot_app", "send", *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_send_to_a_pane_desk_and_to_the_tablet(self):
        proc = pane_desk(self.env, "pane-six")
        try:
            r = self.run_send("pane-six", "-m", "hello from a desk")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to pane-six via pane"), r.stderr)
            buf = self.env.tmux_loaded()[-1][1].decode()
            self.assertTrue(buf.startswith("[from worker-2 via metachat] hello from a desk"), buf)
            r = self.run_send("tablet", "-m", "all done")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to tablet via lane"), r.stderr)
            r = self.run_send("worker-2", "-m", "to myself")
            self.assertEqual(r.returncode, 1)
            self.assertIn("not sent", r.stdout)
            self.assertNotIn(self.env.token, r.stdout + r.stderr)
        finally:
            proc.kill()
            proc.wait()

    def test_send_without_a_mailbox_names_the_shell(self):
        # outside a desk (no METABOT_MAILBOX): the sender is the host's shell,
        # as mc-send spelled it
        r = self.run_send("tablet", "-m", "hi", mailbox=None)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent to tablet via lane"), r.stderr)
        s, j = self.env.get("/api/history")
        frm = j["messages"][-1]["from"]
        self.assertTrue(frm.endswith("-shell"), frm)

    def test_send_needs_a_running_server(self):
        env = dict(os.environ, METACHAT_URL="http://127.0.0.1:1",
                   METACHAT_STATE=str(self.env.state), PYTHONPATH=str(ROOT.parent))
        r = subprocess.run([sys.executable, "-m", "metabot_app", "send", "tablet", "-m", "x"],
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running", r.stderr)
        self.assertNotIn(self.env.token, r.stdout + r.stderr)


class ReplyCmdAdapterTests(unittest.TestCase):
    """11-12 (D2): with an inbox adapter armed, reply_cmd() names the shipped
    subcommand — a desk answering a message runs `metabot-app send`, which every
    install of the wheel has on PATH (the 11-11 tablet finding was mc-send, a
    repo script, named to a desk that had none)."""

    def setUp(self):
        self.env = Env({"inbox_module": str(ECHO_INBOX)}).start()

    def tearDown(self):
        self.env.stop()

    def test_reply_cmd_names_metabot_app_send(self):
        self.assertEqual(server.reply_cmd("pane-seven", "worker-1"), "metabot-app send worker-1 -m")
        self.assertEqual(server.reply_cmd("pane-seven", "tablet"), "metabot-app send tablet -m")

    def test_a_delivered_message_carries_the_shipped_reply_command(self):
        server.INBOX.inner.set_armed(["pane-seven"])
        s, j = self.env.post("/api/send", {"text": "look at this", "target": "pane-seven",
                                           "from": "worker-1"})
        self.assertEqual((s, j["via"]), (200, "inbox"))
        text = server.INBOX.inner.sent()[-1]["text"]
        self.assertIn('(reply: metabot-app send worker-1 -m "...")', text)
        self.assertNotIn("mc-send", text)


if __name__ == "__main__":
    unittest.main()
