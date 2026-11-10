"""Review round two on 1.2 (11-09): the backend findings after feb6e99.

- POST /api/modules/new refuses a label it cannot write (a lone surrogate) or
  should not show (C1 controls, bidi overrides) with 400 ``bad label`` before
  any file is made, and its reply's ``error`` follows module_report's rule (a
  file whose id another user file keeps says so);
- ``serve.running`` reads MetaBot's own signal: ``<metabot home>/serve.lock``
  (an flock while a serve runs, its pid in the file; emptied when it stops);
  the pane is read only when that file is not there;
- a lone surrogate in any catalogue string param is a 400, never a dropped
  connection;
- a bridge's command line never leaves the PC through the engines reads, and
  is never stored in history.sqlite;
- respawn reads the registry entry again inside the spawn lock;
- the default folder is the last folder a claude, cursor or shell desk
  opened in, before the first project's;
- catalogue desk rows carry ``exit`` (the ``[metachat] exit N`` line)."""
import fcntl
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import ECHO_INBOX, Env, FIX, server  # noqa: E402
from metabot_app import catalog  # noqa: E402
from metabot_app import mb  # noqa: E402

def screen(name):
    return (FIX / "screens" / f"{name}.txt").read_text(encoding="utf-8")


class Base(unittest.TestCase):
    config = None

    def setUp(self):
        self.env = Env(self.config).start()

    def tearDown(self):
        self.env.stop()

    def new_sessions(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]

    def row(self, name):
        server.MB._needs.clear()
        server.invalidate()
        s, j = self.env.get("/api/desks")
        self.assertEqual(s, 200)
        return next(d for d in j["desks"] if d["name"] == name)


# ---------------------------------------------------------------- 9: typing into a waiting desk

IDLE_CLAUDE = ("╭──────────────────────────────────────────╮\n"
               "│ >                                        │\n"
               "╰──────────────────────────────────────────╯\n"
               "  ? for shortcuts\n")
WAITING = "the desk is waiting on a question — answer it first"


class PaneDesk(Base):
    """A registered-by-name desk whose pane runs claude (a renamed sleep), with the
    fixture inbox adapter, whose arm types ``/echo-arm <desk>`` as a real one types its own."""
    config = {"inbox_module": str(ECHO_INBOX)}

    def setUp(self):
        super().setUp()
        import subprocess
        self.proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 60"])
        time.sleep(0.2)
        self.env.add_session("fc-pane-one", pid=self.proc.pid, pane=IDLE_CLAUDE)
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "pane-one").write_text("pane-one\n")
        server._sess_cache[0] = 0.0
        server.invalidate()

    def tearDown(self):
        self.proc.kill()
        self.proc.wait()
        super().tearDown()

    def typed(self):
        return [c for c in self.env.calls("tmux")
                if c[:1] in (["send-keys"], ["set-buffer"], ["load-buffer"], ["paste-buffer"])]

    def wait_on(self, text):
        self.env.set_pane("fc-pane-one", text)
        self.env.clear_calls()

    def lane(self):
        with server._local_lock:
            return [dict(e) for e in server._local]


class WaitingDeskChatTests(PaneDesk):
    """A chat line (or another desk's message) is pasted and then sent with
    Enter: into a Claude permission dialog that Enter is "Yes". A desk that
    waits on an ask is never typed into."""

    def test_chat_is_refused_while_a_permission_dialog_waits(self):
        self.wait_on(screen("claude-bash-2.1.319"))
        s, j = self.env.post("/api/send", {"text": "looks good, carry on", "target": "pane-one"})
        self.assertEqual(s, 409, j)
        self.assertFalse(j["ok"])
        self.assertTrue(j["error"].startswith(WAITING), j)
        self.assertIn("❯ 1. Yes", j["error"])
        self.assertEqual(j["reach"], "pane")
        self.assertEqual(self.typed(), [])
        # a handoff and the arm action are typed the same way: refused the same way
        s, j = self.env.post("/api/send", {"text": "take this", "target": "pane-one", "kind": "handoff"})
        self.assertEqual((s, j["ok"]), (409, False))
        s, j = self.env.post("/api/pane", {"target": "pane-one", "action": "arm"})
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertTrue(j["error"].startswith(WAITING), j)
        self.assertEqual(self.typed(), [])

    def test_a_full_screen_picker_swallows_nothing(self):
        self.wait_on("Resume a session\n\n ❯ fix the tests   2 hours ago\n   write the notes   1 day ago\n\n"
                     " Esc to cancel · Enter to select\n")
        s, j = self.env.post("/api/send", {"text": "hello", "target": "pane-one"})
        self.assertEqual((s, j["ok"]), (409, False), j)
        self.assertEqual(self.typed(), [])

    def test_an_idle_claude_is_still_typed_into(self):
        self.wait_on(IDLE_CLAUDE)
        s, j = self.env.post("/api/send", {"text": "hello", "target": "pane-one"})
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "pane", "reach": "pane"}))
        self.assertIn(["send-keys", "-t", "=fc-pane-one:", "Enter"], self.typed())

    def test_desk_to_desk_lands_in_the_lane_untyped(self):
        self.wait_on(screen("claude-bash-2.1.319"))
        before = len(self.lane())
        s, j = self.env.post("/api/send", {"text": "rebase done", "target": "pane-one", "from": "other"})
        self.assertEqual(s, 409, j)
        self.assertEqual((j["ok"], j["via"], j["reach"]), (False, "lane", "pane"))
        self.assertIn("not typed", j["error"])
        self.assertEqual(self.typed(), [])
        ev = self.lane()[before:]
        self.assertEqual([(e["message"], e["target"], e["from"], e["via"], e["typed"]) for e in ev],
                         [("rebase done", "pane-one", "other", "desk", False)])

    def test_the_ask_line_in_the_error_is_cut_and_never_a_link(self):
        self.wait_on("Open https://evil.example/approve?x=1 to continue\n Do you want to proceed? "
                     + "x" * 300 + "\n")
        s, j = self.env.post("/api/send", {"text": "hello", "target": "pane-one"})
        self.assertEqual(s, 409, j)
        self.assertNotIn("://", j["error"])
        self.assertNotIn("evil.example", j["error"])
        self.assertLessEqual(len(j["error"]), len(WAITING) + 4 + 120)


# ---------------------------------------------------------------- 10: arm names the desk's seat

class ArmSeatTests(PaneDesk):
    """The arm goes through the inbox adapter and through the app's paste rules:
    the adapter's command names the desk; a waiting desk and a desk whose pane
    does not run Claude are refused."""

    def test_arm_pastes_the_desk_name(self):
        self.wait_on(IDLE_CLAUDE)
        s, j = self.env.post("/api/pane", {"target": "pane-one", "action": "arm"})
        self.assertEqual((s, j), (200, {"ok": True, "error": ""}))
        self.assertEqual(self.env.tmux_loaded()[-1][1], b"/echo-arm pane-one")
        self.assertIn(["send-keys", "-t", "=fc-pane-one:", "Enter"], self.typed())

    def test_a_waiting_desk_is_not_armed(self):
        self.wait_on(screen("claude-bash-2.1.319"))
        s, j = self.env.post("/api/pane", {"target": "pane-one", "action": "arm"})
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertTrue(j["error"].startswith(WAITING), j)
        self.assertEqual(self.typed(), [])

    def test_only_a_claude_desk_is_armed(self):
        import subprocess
        cur = subprocess.Popen(["bash", "-c", "exec -a cursor-agent sleep 60"])
        try:
            time.sleep(0.2)
            self.env.add_session("fc-pane-two", pid=cur.pid, pane=IDLE_CLAUDE)
            (server.NAMES_DIR / "pane-two").write_text("pane-two\n")
            server._sess_cache[0] = 0.0
            server.invalidate()
            self.env.clear_calls()
            s, j = self.env.post("/api/pane", {"target": "pane-two", "action": "arm"})
            self.assertEqual((s, j), (409, {"ok": False, "error": "arm needs Claude running in this desk"}))
            self.assertEqual(self.typed(), [])
        finally:
            cur.kill()
            cur.wait()


# ---------------------------------------------------------------- 1, 2: modules/new

class ModulesNewLabelTests(Base):
    def test_a_lone_surrogate_label_is_400_and_burns_no_id(self):
        f = self.env.state / "modules" / "mymod.json"
        s, j = self.env.post("/api/modules/new", {"id": "mymod", "label": "a\ud800"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad label"}))
        self.assertFalse(f.exists())
        s, j = self.env.post("/api/modules/new", {"id": "mymod", "label": "Mine"})
        self.assertEqual((s, j["ok"], j["error"]), (200, True, None))
        self.assertEqual(json.loads(f.read_text())["label"], "Mine")

    def test_c1_controls_and_bidi_overrides_are_refused(self):
        for ch in ("\x80", "\x85", "\x9b", "\x9f", "‪", "‮", "⁦", "⁩"):
            s, j = self.env.post("/api/modules/new", {"id": "mymod", "label": f"ab{ch}cd"})
            self.assertEqual((s, j["error"]), (400, "bad label"), repr(ch))
        self.assertFalse((self.env.state / "modules" / "mymod.json").exists())
        # the neighbours of those ranges are labels
        for i, ch in enumerate(("\xa0", " ", "⁰", "é", "◇")):
            s, j = self.env.post("/api/modules/new", {"id": f"ok{chr(97 + i)}", "label": f"a{ch}b"})
            self.assertEqual(s, 200, (repr(ch), j))

    def test_the_reply_names_the_file_that_keeps_the_id(self):
        d = self.env.state / "modules"
        d.mkdir(mode=0o700, exist_ok=True)
        other = {"id": "mymod", "label": "Older", "icon": "x", "order": 120, "kind": "catalog",
                 "phase": 1, "sections": [{"title": "t", "items": [{"catalog": "status", "label": "S"}]}]}
        (d / "zz.json").write_text(json.dumps(other))
        s, j = self.env.post("/api/modules/new", {"id": "mymod", "label": "Mine"})
        self.assertEqual((s, j["ok"]), (200, True))
        self.assertEqual(j["error"], "another file has this id: zz.json")
        s, ext = self.env.get("/api/extend")
        row = next(u for u in ext["modules"]["user"] if u["file"] == j["file"])
        self.assertEqual(row["error"], j["error"])


# ---------------------------------------------------------------- 3: serve

class ServeLockTests(Base):
    def setUp(self):
        super().setUp()
        self._grace = mb.SERVE_START_GRACE
        mb.SERVE_START_GRACE = 0
        self.fd = None

    def tearDown(self):
        mb.SERVE_START_GRACE = self._grace
        if self.fd is not None:
            os.close(self.fd)
        super().tearDown()

    @property
    def lock(self):
        return self.env.mbhome / "serve.lock"

    def hold(self):
        """What a running serve does: flock the file, its pid in it."""
        self.fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, f"{os.getpid()}\n".encode(), 0)

    def serve(self, **body):
        s, j = self.env.post("/api/cli/desk", {"id": "serve", **body})
        self.assertEqual(s, 200, j)
        return j["name"]

    def state(self):
        return self.env.get("/api/extend")[1]["serve"]

    def test_a_serve_that_ended_reads_stopped_on_a_cleared_screen(self):
        name = self.serve()
        self.lock.write_text("")    # serve empties the file when it stops
        self.env.set_pane(f"fc-{name}", "user@pc:~$ \n")
        self.assertEqual(self.state(), {"running": False, "desk": None})
        self.serve(label="serve-again")

    def test_a_killed_serve_leaves_its_pid_but_holds_no_lock(self):
        name = self.serve()
        self.lock.write_text(f"{os.getpid()}\n")   # a live pid that holds no flock on the file
        self.env.set_pane(f"fc-{name}", "user@pc:~$ \n")
        self.assertEqual(self.state(), {"running": False, "desk": None})

    def test_a_held_lock_reads_running_whatever_the_screen_shows(self):
        name = self.serve()
        self.hold()
        self.env.set_pane(f"fc-{name}", "user@pc:~$ \n")
        self.assertEqual(self.state(), {"running": True, "desk": name})
        s, j = self.env.post("/api/cli/desk", {"id": "serve", "label": "serve-again"})
        self.assertEqual((s, j["error"]), (409, "serve already runs"))

    def test_a_serve_from_elsewhere_runs_with_no_desk(self):
        self.hold()
        self.assertEqual(self.state(), {"running": True, "desk": None})
        s, j = self.env.post("/api/cli/desk", {"id": "serve"})
        self.assertEqual((s, j["error"]), (409, "serve already runs"))

    def test_a_desk_that_has_just_opened_counts_while_serve_starts(self):
        mb.SERVE_START_GRACE = 30
        self.lock.write_text("")    # an earlier serve stopped; this one has not locked yet
        name = self.serve()
        self.assertEqual(self.state(), {"running": True, "desk": name})
        # its exit line on screen ends the grace at once
        self.env.set_pane(f"fc-{name}", "[metachat] exit 2\nuser@pc:~$ \n")
        self.assertEqual(self.state(), {"running": False, "desk": None})

    def test_lock_state_reader(self):
        self.assertIsNone(mb.serve_lock_state(str(self.env.mbhome)))   # never ran here: unknown
        self.lock.write_text("")
        self.assertEqual(mb.serve_lock_state(str(self.env.mbhome)), "free")
        self.lock.write_text("not a pid\n")
        self.assertIsNone(mb.serve_lock_state(str(self.env.mbhome)))
        self.hold()
        self.assertEqual(mb.serve_lock_state(str(self.env.mbhome)), "held")


# ---------------------------------------------------------------- 4: surrogates in catalogue params

class SurrogateParamTests(Base):
    def test_reads_and_desks_refuse_a_lone_surrogate(self):
        s, j = self.env.post("/api/cli/read", {"id": "suggest", "params": {"task": "a\ud800"}})
        self.assertEqual(s, 400, j)
        self.assertRegex(j["error"], r"^bad task")
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new-ladder", "params": {
            "name": "w", "folder": str(self.env.projects / "alpha"), "prompt": "a\udfff"}})
        self.assertEqual(s, 400, j)
        self.assertRegex(j["error"], r"^bad prompt")
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new-ladder", "params": {
            "name": "w", "folder": str(self.env.projects / "alpha") + "\ud800", "prompt": "p"}})
        self.assertEqual(s, 400, j)
        self.assertRegex(j["error"], r"^bad folder")
        self.assertEqual(self.new_sessions(), [])

    def test_a_desk_name_with_one_gets_a_reply(self):
        """desk_slug hashed the raw name as UTF-8: these three dropped the connection."""
        for path, body in (("/api/kill", {"name": "a\ud800"}), ("/api/keys", {"name": "a\ud800", "key": "enter"}),
                           ("/api/pane", {"target": "a\ud800", "action": "arm"})):
            s, j = self.env.post(path, body)
            self.assertIn(s, (400, 404, 409), (path, j))
            self.assertFalse(j["ok"])

    def test_every_string_type_refuses_one(self):
        ctx = server.MB.catalog_ctx()
        for t in ("text", "path", "url", "id", "slug", "enum", "decimal", "sha256", "receipt"):
            ok, err = catalog.validate_param({"name": "x", "type": t, "values": ["a\ud800"]}, "a\ud800", ctx)
            self.assertFalse(ok, t)
        # a pair is one character, never refused
        self.assertEqual(catalog.validate_param({"name": "x", "type": "text"}, "a\U0001f600", ctx),
                         (True, "a\U0001f600"))


# ---------------------------------------------------------------- 5: bridge command lines

SECRET = "--token=s3cr3t-bridge-pass"


class BridgeRedactionTests(Base):
    def stored(self):
        out = b""
        for f in self.env.state.glob("history.sqlite*"):
            out += f.read_bytes()
        return out

    def test_engines_reads_never_carry_a_bridge_command(self):
        for cid in ("engines.list", "engines.manifest"):
            s, j = self.env.post("/api/cli/read", {"id": cid})
            self.assertEqual(s, 200, j)
            self.assertNotIn("s3cr3t", j["stdout"] + j["stderr"], cid)
            self.assertNotIn("/opt/bridges", j["stdout"], cid)
            self.assertIn("mybridge", j["stdout"])
            self.assertIn("bridge from demo@market: <command on the PC>", j["stdout"])
            self.assertIn("claude  /usr/bin/claude", j["stdout"])   # a built-in line is kept
            self.assertIn("(its command is not found)", j["stdout"])
        self.assertNotIn(b"s3cr3t", self.stored())
        s, ev = self.env.get("/api/events?limit=5")
        self.assertNotIn("s3cr3t", json.dumps(ev))

    def test_the_redaction_rule(self):
        out = mb.redact_bridges("* claude  /usr/bin/claude\n"
                                "  longbridgename bridge: /x/run --key=abc (x not found)\n"
                                "  b       bridge from p@m: python3 -c 'print(1)'\n"
                                "    read-only  yes\n")
        self.assertEqual(out, "* claude  /usr/bin/claude\n"
                              "  longbridgename bridge: <command on the PC> (its command is not found)\n"
                              "  b       bridge from p@m: <command on the PC>\n"
                              "    read-only  yes\n")

    def test_other_reads_are_untouched(self):
        s, j = self.env.post("/api/cli/read", {"id": "status"})
        self.assertEqual(s, 200, j)
        self.assertNotIn("<command on the PC>", j["stdout"])


# ---------------------------------------------------------------- 6: respawn racing a new sign-in

class RespawnRaceTests(Base):
    def setUp(self):
        super().setUp()
        self.work = self.env.state / "accounts" / "claude" / "work"
        self.work.mkdir(parents=True, mode=0o700)
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": str(self.work), "max_at_once": 3}]))
        self._account_env = mb.account_env

    def tearDown(self):
        mb.account_env = self._account_env
        super().tearDown()

    def end_session(self, name):
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"][f"fc-{name}"]
        self.env.tmux_state.write_text(json.dumps(st))

    def between(self, act):
        """Run ``act`` after respawn has read the entry, before it takes the lock."""
        real, once = self._account_env, []

        def hooked(*a, **kw):
            if not once:   # once: the server's own spawn below calls it too
                once.append(1)
                act()
            return real(*a, **kw)
        mb.account_env = hooked

    def test_a_new_sign_in_in_between_wins(self):
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work", "signin": True})
        self.assertEqual(s, 200, j)
        old = j["name"]
        self.end_session(old)
        news = []
        self.between(lambda: news.append(self.env.post("/api/spawn", {
            "kind": "claude", "account": "work", "signin": True, "label": "signin-again"})))
        self.env.clear_calls()
        ok, err = mb.respawn(server.MB, old)
        self.assertEqual(news[0][0], 200, news)
        self.assertEqual((ok, err), (False, "no such desk"))
        self.assertEqual([c[c.index("-s") + 1] for c in self.new_sessions()], ["fc-signin-again"])

    def test_a_desk_closed_in_between_never_comes_back(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "plain",
                                            "cwd": str(self.env.projects / "alpha")})
        self.assertEqual(s, 200, j)
        self.end_session("plain")
        self.between(lambda: mb.reg_delete(server.MB, "plain"))
        self.env.clear_calls()
        self.assertEqual(mb.respawn(server.MB, "plain"), (False, "no such desk"))
        self.assertEqual(self.new_sessions(), [])

    def test_an_entry_changed_in_between_is_not_respawned_from_the_old_read(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "plain",
                                            "cwd": str(self.env.projects / "alpha")})
        self.end_session("plain")

        def change():
            e = mb.reg_read(server.MB, "plain")
            e["cwd"] = str(self.env.projects / "beta")
            mb.reg_write(server.MB, e)
        self.between(change)
        self.env.clear_calls()
        self.assertEqual(mb.respawn(server.MB, "plain"), (False, "desk changed"))
        self.assertEqual(self.new_sessions(), [])


# ---------------------------------------------------------------- 7: default folder = last used

class LastFolderTests(Base):
    def setUp(self):
        super().setUp()
        cfg = json.loads(self.env.config.read_text())
        cfg["projects"] = [{"name": "p", "paths": [str(self.env.projects / "alpha"),
                                                   str(self.env.projects / "beta")]}]
        self.env.config.write_text(json.dumps(cfg))
        server.configure()

    def meta_folder(self):
        return self.env.get("/api/meta")[1]["default_folder"]

    def spawn(self, kind, cwd=None):
        body = {"kind": kind}
        if cwd is not None:
            body["cwd"] = str(cwd)
        s, j = self.env.post("/api/spawn", body)
        self.assertEqual(s, 200, j)
        return j["name"]

    @property
    def last(self):
        return self.env.state / "last-folder"

    def test_the_last_folder_used_comes_before_the_first_project(self):
        alpha, beta = self.env.projects / "alpha", self.env.projects / "beta"
        self.assertEqual(self.meta_folder(), str(alpha))
        self.spawn("cursor", beta)
        self.assertEqual(self.meta_folder(), str(beta))
        self.assertEqual(self.last.stat().st_mode & 0o777, 0o600)
        name = self.spawn("claude")
        self.assertEqual(mb.reg_read(server.MB, name)["cwd"], str(beta))
        # a shell in home leaves it; a shell in a project folder sets it
        self.spawn("shell")
        self.assertEqual(self.meta_folder(), str(beta))
        self.spawn("shell", alpha)
        self.assertEqual(self.meta_folder(), str(alpha))
        # a shell in MetaChat's modules folder (it passes the rules) is not a project folder
        self.env.post("/api/modules/new", {"id": "mine", "label": "Mine"})
        self.spawn("shell", self.env.state / "modules")
        self.assertEqual(self.meta_folder(), str(alpha))

    def test_config_default_folder_still_wins(self):
        alpha, beta = self.env.projects / "alpha", self.env.projects / "beta"
        cfg = json.loads(self.env.config.read_text())
        cfg["default_folder"] = str(alpha)
        self.env.config.write_text(json.dumps(cfg))
        server.configure()
        self.spawn("claude", beta)
        self.assertEqual(self.meta_folder(), str(alpha))

    def test_a_recorded_folder_that_fails_the_rules_is_skipped(self):
        alpha = self.env.projects / "alpha"
        for bad in (str(self.env.home), "/etc", "relative/x", str(self.env.projects / "gone"),
                    str(self.env.home / ".ssh"), "x" * 5000):
            self.last.write_text(bad + "\n")
            self.assertEqual(self.meta_folder(), str(alpha), bad[:40])
        target = self.env.root / "elsewhere"
        target.write_text(str(self.env.projects / "beta"))
        self.last.unlink()
        os.symlink(target, self.last)   # never followed
        self.assertEqual(self.meta_folder(), str(alpha))

    def test_a_catalogue_desk_is_not_a_folder_used(self):
        beta = self.env.projects / "beta"
        s, j = self.env.post("/api/cli/desk", {"id": "serve", "cwd": str(beta)})
        self.assertEqual(s, 200, j)
        self.assertFalse(self.last.exists())


# ---------------------------------------------------------------- 8: catalogue desks carry exit

class CatalogueExitTests(Base):
    def test_a_catalogue_desk_row_carries_its_exit(self):
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.approve", "params": {"ref": "w"}})
        self.assertEqual(s, 200, j)
        name = j["name"]
        self.env.set_pane(f"fc-{name}", "$ metabot workflow approve w\nApprove? [y/N] ")
        self.assertIsNone(self.row(name)["exit"])
        self.env.set_pane(f"fc-{name}", "$ metabot x\nok\n\n[metachat] exit 0\nuser@pc:~$ \n")
        self.assertEqual(self.row(name)["exit"], 0)
        self.env.set_pane(f"fc-{name}", "$ metabot x\nno\n\n[metachat] exit 3\nuser@pc:~$ \n")
        self.assertEqual(self.row(name)["exit"], 3)

    def test_other_desks_carry_no_exit(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "plain"})
        self.assertEqual(s, 200, j)
        self.env.set_pane("fc-plain", "[metachat] exit 0\nuser@pc:~$ \n")
        self.assertNotIn("exit", self.row("plain"))


if __name__ == "__main__":
    unittest.main()
