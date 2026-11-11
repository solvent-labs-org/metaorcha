"""Contract 25.3 B1-B5: sign-in desks, accounts that say whether they are signed
in, one-tap keys, the default folder, and needs_you on the sign-in screens.

The screens in tests/fixtures/screens are the lines the contract lists (Claude
Code 2.1.319, cursor-agent 2026.11.05), with made-up links of the real shape.
A line written inline below as a "pattern check" is not a captured screen: it
only proves the pattern the contract names."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server, ws_read_frame  # noqa: E402
from metabot_app import mb  # noqa: E402

SCREENS = FIX / "screens"


def screen(name):
    return (SCREENS / f"{name}.txt").read_text(encoding="utf-8")


def claude_link():
    """The made-up link in claude-code.txt: four lines Claude broke with hard newlines."""
    lines = screen("claude-code").splitlines()
    assert lines[3].startswith("https://claude.com/") and lines[7] == "", lines
    return "".join(lines[3:7])


def cursor_link():
    return next(ln for ln in screen("cursor-login").splitlines() if ln.startswith("https://"))


def unset_pairs(extra=()):
    out = []
    for n in mb.unset_list(server.MB, extra):
        out += ["-u", n]
    return out


class SignInBase(unittest.TestCase):
    config = None

    def setUp(self):
        self.env = Env(self.config).start()
        self.work = self.env.state / "accounts" / "claude" / "work"
        self.work.mkdir(parents=True, mode=0o700)
        os.chmod(self.work, 0o700)
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": str(self.work), "max_at_once": 3}]))

    def tearDown(self):
        self.env.stop()

    def signin(self, kind="claude", account="work", **extra):
        return self.env.post("/api/spawn", {"kind": kind, "account": account, "signin": True, **extra})

    def row(self, name):
        server.MB._needs.clear()
        server.invalidate()
        s, j = self.env.get("/api/desks")
        self.assertEqual(s, 200)
        return next(d for d in j["desks"] if d["name"] == name)

    def show(self, name, text):
        self.env.set_pane(f"fc-{name}", text)
        return self.row(name)

    def new_sessions(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]

    def sent(self):
        """Every tmux call that types into a pane or loads a buffer."""
        return [c for c in self.env.calls("tmux")
                if c[:1] in (["send-keys"], ["set-buffer"], ["load-buffer"], ["paste-buffer"])]


class SignInSpawnTests(SignInBase):
    """B1: POST /api/spawn {kind, account, signin:true}."""

    def test_sign_in_needs_an_account_and_a_cli_kind(self):
        for body in ({"kind": "claude", "signin": True}, {"kind": "claude", "account": "", "signin": True},
                     {"kind": "claude", "account": "default", "signin": True},
                     {"kind": "cursor", "account": None, "signin": True}):
            s, j = self.env.post("/api/spawn", body)
            self.assertEqual((s, j), (400, {"ok": False, "error": "sign-in needs an account"}), body)
        s, j = self.signin(kind="shell")
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad kind"}))
        s, j = self.signin(account="ghost")
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such account"}))
        s, j = self.signin(account="Bad Name")
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad account"}))
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work", "signin": "yes"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad signin"}))
        self.assertEqual(self.new_sessions(), [])

    def test_a_codex_login_is_not_a_claude_sign_in(self):
        codex = self.env.state / "accounts" / "codex" / "cx"
        codex.mkdir(parents=True, mode=0o700)
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "cx", "engine": "codex", "home": str(codex), "max_at_once": 1}]))
        s, j = self.signin(account="cx")
        self.assertEqual((s, j), (400, {"ok": False, "error": "not a claude login"}))
        self.assertEqual(self.new_sessions(), [])

    def test_a_plain_claude_desk_refuses_a_codex_login_too(self):
        """Follow-up D: Claude never runs on a codex login's folder, signed in or not."""
        codex = self.env.state / "accounts" / "codex" / "cx"
        codex.mkdir(parents=True, mode=0o700)
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "cx", "engine": "codex", "home": str(codex), "max_at_once": 1},
             {"name": "work", "engine": "claude", "home": str(self.work), "max_at_once": 3}]))
        self.env.clear_calls()
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "cx"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "not a claude login"}))
        self.assertEqual(self.new_sessions(), [])
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work"})
        self.assertEqual(s, 200, j)

    def test_claude_sign_in_desk_argv_folder_and_registry(self):
        s, j = self.signin()
        self.assertEqual((s, j), (200, {"ok": True, "name": "signin-claude-work"}))
        folder = self.env.state / "signin"
        self.assertEqual(self.new_sessions()[-1], [
            "new-session", "-d", "-s", "fc-signin-claude-work", "-c", str(folder),
            "-e", f"METABOT_HOME={self.env.mbhome}", "-e", "METABOT_MAILBOX=signin-claude-work", "-e", f"CLAUDE_CONFIG_DIR={self.work}",
            "--", "env", *unset_pairs(), "bash", "-lc", mb.FIXED_SCRIPT, "metachat", "claude"])
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(folder.iterdir()), [])            # kept empty
        reg = json.loads((self.env.state / "desks" / "signin-claude-work.json").read_text())
        self.assertEqual((reg["kind"], reg["account"], reg["signin"], reg["cwd"], reg["argv"]),
                         ("claude", "work", True, str(folder), ["claude"]))
        self.assertFalse((server.NAMES_DIR / "signin-claude-work").exists())   # a sign-in desk is never named
        s, j = self.signin()
        self.assertEqual((s, j), (409, {"ok": False, "error": "already signing in"}))
        d = self.row("signin-claude-work")
        self.assertEqual(d["signin"], {"engine": "claude", "account": "work", "step": "starting",
                                       "url": None, "exit": None})
        caps = [c for c in self.env.calls("tmux") if c[:1] == ["capture-pane"]]
        self.assertIn(["capture-pane", "-p", "-J", "-S", "-40", "-t", "=fc-signin-claude-work:"], caps)
        s, j = self.env.get("/api/events?limit=5")
        spawn = [e for e in j["events"] if e["kind"] == "spawn"][0]
        self.assertTrue(spawn["data"].get("signin"))
        # a plain desk carries no signin field
        self.env.post("/api/spawn", {"kind": "shell", "label": "plain"})
        self.assertNotIn("signin", self.row("plain"))

    def test_a_dead_sign_in_desk_gives_its_name_back(self):
        self.assertEqual(self.signin()[0], 200)
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-signin-claude-work"]
        self.env.tmux_state.write_text(json.dumps(st))
        s, j = self.signin()
        self.assertEqual((s, j), (200, {"ok": True, "name": "signin-claude-work"}))
        names = sorted(p.stem for p in (self.env.state / "desks").glob("*.json"))
        self.assertEqual(names, ["signin-claude-work"])
        s, j = self.signin(label="again")      # a label is honoured; the live one still refuses
        self.assertEqual((s, j), (409, {"ok": False, "error": "already signing in"}))

    def test_cursor_sign_in_desk_runs_cursor_agent_login_in_its_folder(self):
        s, j = self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(s, 200, j)
        s, j = self.signin(kind="cursor", account="alt")
        self.assertEqual((s, j), (200, {"ok": True, "name": "signin-cursor-alt"}))
        acc = self.env.state / "accounts" / "cursor" / "alt"
        self.assertEqual(self.new_sessions()[-1], [
            "new-session", "-d", "-s", "fc-signin-cursor-alt", "-c", str(self.env.state / "signin"),
            "-e", f"METABOT_HOME={self.env.mbhome}", "-e", "METABOT_MAILBOX=signin-cursor-alt", "-e", f"XDG_CONFIG_HOME={acc}/config",
            "-e", f"GH_CONFIG_DIR={self.env.home}/.config/gh", "-e", f"CURSOR_CONFIG_DIR={self.env.home}/.cursor",
            "--", "env", *unset_pairs(mb.CURSOR_UNSET), "bash", "-lc", mb.FIXED_SCRIPT, "metachat",
            "cursor-agent", "login"])
        self.assertEqual(self.signin(kind="cursor", account="alt")[1]["error"], "already signing in")
        self.assertEqual(self.signin()[0], 200)    # another engine's account is its own sign-in
        s, j = self.signin(label="mine", account="work")
        self.assertEqual(s, 409)

    def test_a_label_names_the_desk(self):
        s, j = self.signin(label="claude-in")
        self.assertEqual((s, j), (200, {"ok": True, "name": "claude-in"}))
        self.assertEqual(self.row("claude-in")["signin"]["account"], "work")

    def test_sign_in_desk_takes_no_chat(self):
        proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
        try:
            time.sleep(0.2)
            os.environ["FAKE_PANE_PID"] = str(proc.pid)
            try:
                self.assertEqual(self.signin()[0], 200)
            finally:
                os.environ.pop("FAKE_PANE_PID", None)
            d = self.row("signin-claude-work")
            self.assertEqual((d["pane"], d["cli"]), (True, "claude"))
            self.assertEqual(d["reach"], "idle")      # never "pane": chat text never types into a sign-in
            self.env.clear_calls()
            s, j = self.env.post("/api/send", {"text": "hello", "target": "signin-claude-work"})
            self.assertEqual((s, j["error"]), (409, "a sign-in desk takes no chat — use its sign-in card"))
            s, j = self.env.post("/api/pane", {"target": "signin-claude-work", "action": "arm"})
            self.assertEqual(s, 409)
            for forbidden in ("set-buffer", "load-buffer", "paste-buffer", "send-keys"):
                self.assertNotIn(forbidden, [c[0] for c in self.env.calls("tmux")])
        finally:
            proc.kill()
            proc.wait()

    def test_kill_leaves_a_names_file_it_never_wrote(self):
        self.assertEqual(self.signin()[0], 200)
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "signin-claude-work").write_text("signin-claude-work\n")
        s, j = self.env.post("/api/kill", {"name": "signin-claude-work", "confirm": True})
        self.assertEqual((s, j), (200, {"ok": True}))
        self.assertTrue((server.NAMES_DIR / "signin-claude-work").is_file())

    def test_a_dead_sign_in_desk_comes_back_in_its_own_folder(self):
        self.assertEqual(self.signin()[0], 200)
        first = self.new_sessions()[-1]
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-signin-claude-work"]
        self.env.tmux_state.write_text(json.dumps(st))
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=signin-claude-work")
        self.assertEqual(status, 101)
        op, payload, rest = ws_read_frame(s, rest)
        self.assertIn(b"attached fc-signin-claude-work", payload)
        s.close()
        time.sleep(0.2)
        self.assertEqual(self.new_sessions(), [first])


class SignInStepTests(SignInBase):
    """B1: the step and the link, read from the last 40 pane lines."""

    def test_claude_steps_from_the_captured_screens(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        for fixture, step in (("claude-style", "style"), ("claude-method", "method"),
                              ("claude-code", "code"), ("claude-trust", "trust")):
            d = self.show(n, screen(fixture))
            self.assertEqual(d["signin"]["step"], step, fixture)
        d = self.show(n, screen("claude-code"))
        self.assertEqual(d["signin"]["url"], claude_link())
        self.assertTrue(d["signin"]["url"].endswith("state=" + "B" * 39))
        # pattern checks (not captured screens): success and notes
        for text, step in (("Login successful", "success"), ("Logged in as someone@example.test", "success"),
                           ("Security notes:", "notes"), (" Press Enter to continue…", "notes"),
                           ("Login successful. Press Enter to continue…", "notes")):   # a tie: the later step
            self.assertEqual(self.show(n, text + "\n")["signin"]["step"], step, text)

    def test_the_latest_screen_wins_within_the_last_forty_lines(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        self.assertEqual(self.show(n, screen("claude-style") + screen("claude-method"))["signin"]["step"],
                         "method")
        d = self.show(n, screen("claude-method") + screen("claude-code"))
        self.assertEqual((d["signin"]["step"], d["signin"]["url"]), ("code", claude_link()))
        self.assertEqual(self.show(n, screen("claude-code") + screen("claude-trust"))["signin"]["step"],
                         "trust")
        # the code screen 41+ lines up is out of the window: nothing matched, not signed in
        d = self.show(n, screen("claude-code") + "\n".join(["output"] * 45) + "\n")
        self.assertEqual(d["signin"], {"engine": "claude", "account": "work", "step": "starting", "url": None,
                                       "exit": None})
        # blank rows under the screen (a tall pane) are not lines of it
        d = self.show(n, screen("claude-method") + "\n" * 60)
        self.assertEqual(d["signin"]["step"], "method")

    def test_link_rules(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        code = "\n Paste code here if prompted >\n"
        cases = [
            ("https://evil.test/cai/oauth/authorize?code=true", None),
            ("https://claude.com.evil.test/cai/oauth", None),
            ("https://evilclaude.com/cai/oauth", None),
            ("http://claude.com/cai/oauth", None),                       # not https
            ("https://claude.ai/oauth/authorize?code=true", "https://claude.ai/oauth/authorize?code=true"),
            ("https://platform.claude.com/oauth/x?a=1", "https://platform.claude.com/oauth/x?a=1"),
            ("https://console.anthropic.com/oauth/x?a=1", "https://console.anthropic.com/oauth/x?a=1"),
            ("  https://claude.com/x?a=1 (c to copy)", "https://claude.com/x?a=1"),   # the link ends at a space
            ("https://claude.com/x?a=\n1&b=2\n  3&c=4  \nnot part of it", "https://claude.com/x?a=1&b=23&c=4"),
            ("old https://claude.com/old\nhttps://claude.com/new?x=1", "https://claude.com/new?x=1"),
        ]
        for text, url in cases:
            d = self.show(n, text + code)
            self.assertEqual(d["signin"]["url"], url, text)
            self.assertEqual(d["signin"]["step"], "code", text)
        long = "https://claude.com/" + "a" * 3000 + "\n" + "b" * 3000
        d = self.show(n, long + code)
        self.assertEqual(len(d["signin"]["url"]), 4096)
        self.assertTrue(d["signin"]["url"].startswith("https://claude.com/aaa"))

    def test_cursor_steps(self):
        s, j = self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(self.signin(kind="cursor", account="alt")[0], 200)
        n = "signin-cursor-alt"
        d = self.show(n, screen("cursor-status"))
        self.assertEqual(d["signin"]["step"], "starting")       # "Not logged in" is no sign-in
        d = self.show(n, screen("cursor-login"))
        self.assertEqual(d["signin"], {"engine": "cursor", "account": "alt", "step": "link",
                                       "url": cursor_link(), "exit": None})
        self.assertEqual(self.show(n, "Logged in as someone@example.test\n")["signin"]["step"], "success")
        self.assertEqual(self.show(n, "Authentication successful\n")["signin"]["step"], "success")
        auth = self.env.state / "accounts" / "cursor" / "alt" / "config" / "cursor"
        auth.mkdir(parents=True)
        os.mkfifo(auth / "auth.json")          # stat only: opening a fifo would hang the request
        self.assertEqual(self.show(n, "")["signin"]["step"], "ready")

    def test_ready_when_nothing_matched_and_the_account_is_signed_in(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        self.assertEqual(self.show(n, "")["signin"]["step"], "starting")
        os.mkfifo(self.work / ".credentials.json")
        self.assertEqual(self.show(n, "")["signin"]["step"], "ready")
        self.assertEqual(self.show(n, screen("claude-method"))["signin"]["step"], "method")


class AccountsReadyTests(SignInBase):
    """B2: ready by stat alone; the default sign-ins; login.set."""

    def test_ready_rows_and_defaults_are_stat_only(self):
        codex = self.env.state / "accounts" / "codex" / "cx"
        codex.mkdir(parents=True, mode=0o700)
        os.chmod(codex, 0o700)
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": str(self.work), "max_at_once": 3},
             {"name": "cx", "engine": "codex", "home": str(codex), "max_at_once": 1}]))
        s, j = self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(s, 200, j)
        s, j = self.env.get("/api/accounts")
        self.assertEqual(s, 200)
        self.assertEqual([(a["name"], a["ready"]) for a in j["claude"]], [("work", False), ("cx", False)])
        self.assertEqual(j["cursor"], [{"name": "alt", "folder": str(self.env.state / "accounts/cursor/alt"),
                                        "proven": False, "ready": False}])
        self.assertEqual(j["defaults"], {"claude": {"ready": False}, "cursor": {"ready": False}})
        # every sign-in file is a fifo: an open() would block this request
        os.mkfifo(self.work / ".credentials.json")
        os.mkfifo(codex / "auth.json")
        cur = self.env.state / "accounts" / "cursor" / "alt" / "config" / "cursor"
        cur.mkdir(parents=True)
        os.mkfifo(cur / "auth.json")
        os.mkfifo(self.env.home / ".claude" / ".credentials.json")
        (self.env.home / ".config" / "cursor").mkdir(parents=True)
        os.mkfifo(self.env.home / ".config" / "cursor" / "auth.json")
        s, j = self.env.get("/api/accounts")
        self.assertEqual([(a["name"], a["ready"]) for a in j["claude"]], [("work", True), ("cx", True)])
        self.assertTrue(j["cursor"][0]["ready"])
        self.assertEqual(j["defaults"], {"claude": {"ready": True}, "cursor": {"ready": True}})
        os.chmod(self.work, 0o755)            # others can enter it: not a sign-in folder MetaBot runs on
        s, j = self.env.get("/api/accounts")
        self.assertFalse(j["claude"][0]["ready"])

    def test_a_login_whose_last_run_could_not_sign_in_reads_sign_in_again(self):
        """11-09 matrix: a login past its time limit is still 'ready' by stat (its
        file is there); its last run says it could not sign in. Learnt from the
        run rows MetaBot already wrote, never from the sign-in file."""
        os.mkfifo(self.work / ".credentials.json")   # a fifo: opening it would block
        rows = self.env.mbhome / "runs.jsonl"

        def add(**r):
            with open(rows, "a") as f:
                f.write(json.dumps(dict({"cwd": "/x", "engine": "claude"}, **r)) + "\n")

        def work():
            s, j = self.env.get("/api/accounts")
            self.assertEqual(s, 200, j)
            return [a for a in j["claude"] if a["name"] == "work"][0]
        add(run_id="r1", login="work", status="ok", finished_at="2026-11-09T10:00:00Z")
        self.assertTrue(work()["ready"])
        self.assertNotIn("signin_failed", work())
        add(run_id="r2", login="work", status="error", finished_at="2026-11-09T11:00:00Z",
            error="claude: success: Failed to authenticate: this login reached its time limit and cannot be renewed. "
                  "Run `claude auth login` to sign in again")
        w = work()
        self.assertFalse(w["ready"])
        self.assertEqual(w["signin_failed"]["at"], "2026-11-09T11:00:00Z")
        self.assertIn("Failed to authenticate", w["signin_failed"]["why"])
        self.assertLessEqual(len(w["signin_failed"]["why"]), 160)
        add(run_id="r3", login="other", status="ok", finished_at="2026-11-09T11:30:00Z")
        self.assertFalse(work()["ready"], "another login's run says nothing about this one")
        add(run_id="r4", login="work", status="error", error="max cost reached", finished_at="2026-11-09T11:40:00Z")
        self.assertTrue(work()["ready"], "a run that failed for another reason did sign in")
        add(run_id="r5", login="work", status="error", error="Not logged in · Please run /login", finished_at="2026-11-09T12:00:00Z")
        self.assertFalse(work()["ready"])
        add(run_id="r6", login="work", status="ok", finished_at="2026-11-09T12:30:00Z")
        self.assertTrue(work()["ready"], "a later run that worked clears it")
        self.assertNotIn("signin_failed", work())
        # the default sign-in (no login named) is read the same way, per engine
        os.mkfifo(self.env.home / ".claude" / ".credentials.json")
        add(run_id="r7", login=None, engine="claude", status="error", error="Not logged in · Please run /login",
            finished_at="2026-11-09T13:00:00Z")
        s, j = self.env.get("/api/accounts")
        self.assertFalse(j["defaults"]["claude"]["ready"])
        self.assertEqual(j["defaults"]["claude"]["signin_failed"]["at"], "2026-11-09T13:00:00Z")
        self.assertNotIn("signin_failed", j["defaults"]["cursor"])
        self.assertTrue(work()["ready"])

    def test_login_set_is_a_desk_entry(self):
        s, j = self.env.post("/api/cli/desk", {"id": "login.set", "params": {"name": "work", "n": 3}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "login", "set", "--max-at-once", "3", "work"])
        for params, err in (({"name": "work", "n": 0}, "bad n: out of range"),
                            ({"name": "work", "n": 6}, "bad n: out of range"),
                            ({"name": "work", "n": "x"}, "bad n: not an int"),
                            ({"name": "Work", "n": 2}, "bad name: not a slug")):
            s, j = self.env.post("/api/cli/desk", {"id": "login.set", "params": params})
            self.assertEqual((s, j), (400, {"ok": False, "error": err}), params)


class KeysTests(SignInBase):
    """B3: POST /api/keys {name, key} | {name, keys:[...]} | {name, text}."""

    NAMES = {"enter": "Enter", "esc": "Escape", "up": "Up", "down": "Down", "tab": "Tab",
             "1": "1", "2": "2", "3": "3", "y": "y", "n": "n", "q": "q"}

    def setUp(self):
        super().setUp()
        self.assertEqual(self.env.post("/api/spawn", {"kind": "shell", "label": "k1"})[0], 200)
        # review 1.2: keys answer only a desk that waits now (KeysFreshTests in test_review_1_2)
        self.env.set_pane("fc-k1", "install it? [y/N] ")

    def keys(self, **body):
        return self.env.post("/api/keys", body)

    def test_one_key_is_one_send_keys_with_the_tmux_name(self):
        for key, name in self.NAMES.items():
            self.env.clear_calls()
            self.assertEqual(self.keys(name="k1", key=key), (200, {"ok": True}), key)
            self.assertEqual(self.sent(), [["send-keys", "-t", "=fc-k1:", name]], key)

    def test_a_list_of_keys_goes_in_order_in_one_call(self):
        self.env.clear_calls()
        self.assertEqual(self.keys(name="k1", keys=["y", "enter"]), (200, {"ok": True}))
        self.assertEqual(self.sent(), [["send-keys", "-t", "=fc-k1:", "y", "Enter"]])
        self.env.clear_calls()
        self.assertEqual(self.keys(name="k1", keys=["down", "down", "up", "enter"])[0], 200)
        self.assertEqual(self.sent(), [["send-keys", "-t", "=fc-k1:", "Down", "Down", "Up", "Enter"]])
        self.env.clear_calls()
        for bad in (["y", "n", "y", "n", "enter"], [], ["y", "bogus"], ["y", "Enter"], "y", [1], ["-l"],
                    {"0": "y"}):
            self.assertEqual(self.keys(name="k1", keys=bad), (400, {"ok": False, "error": "bad keys"}), bad)
        self.assertEqual(self.sent(), [])

    def test_refusals_send_nothing(self):
        self.env.clear_calls()
        for body, want in (({"key": "y"}, (400, "bad name")),
                           ({"name": "", "key": "y"}, (400, "bad name")),
                           ({"name": "ghost", "key": "y"}, (404, "no such desk")),
                           ({"name": "k1"}, (400, "send one of key, keys or text")),
                           ({"name": "k1", "key": "y", "keys": ["y"]}, (400, "send one of key, keys or text")),
                           ({"name": "k1", "key": "y", "text": "x"}, (400, "send one of key, keys or text")),
                           ({"name": "k1", "key": "F1"}, (400, "bad key")),
                           ({"name": "k1", "key": "-l"}, (400, "bad key")),
                           ({"name": "k1", "key": ["y"]}, (400, "bad key"))):
            s, j = self.keys(**body)
            self.assertEqual((s, j), (want[0], {"ok": False, "error": want[1]}), body)
        self.assertEqual(self.sent(), [])
        s, j = self.env.post("/api/keys", {"name": "k1", "key": "y"}, headers={"X-MetaChat": None})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))
        self.assertEqual(self.sent(), [])

    def test_a_registered_desk_whose_session_ended(self):
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-k1"]
        self.env.tmux_state.write_text(json.dumps(st))
        self.env.clear_calls()
        self.assertEqual(self.keys(name="k1", key="enter"), (409, {"ok": False, "error": "desk not running"}))
        self.assertEqual(self.sent(), [])

    def test_a_known_tmux_desk_takes_keys(self):
        self.env.add_session("fc-legacy-k")
        self.env.set_pane("fc-legacy-k", "Do you want to proceed?\n")
        self.env.clear_calls()
        self.assertEqual(self.keys(name="legacy-k", key="esc"), (200, {"ok": True}))
        self.assertEqual(self.sent(), [["send-keys", "-t", "=fc-legacy-k:", "Escape"]])

    def test_text_goes_only_to_a_sign_in_desk_at_the_code_step(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        self.env.clear_calls()
        self.assertEqual(self.keys(name="k1", text="abc"), (409, {"ok": False, "error": "not at the code step"}))
        self.env.set_pane(f"fc-{n}", screen("claude-method"))
        self.assertEqual(self.keys(name=n, text="abc"), (409, {"ok": False, "error": "not at the code step"}))
        self.assertEqual(self.sent(), [])
        self.env.set_pane(f"fc-{n}", screen("claude-code"))
        code = "-x9Z_code#STATE-123"          # a leading dash is still text, never a tmux flag
        self.assertEqual(self.keys(name=n, text=code), (200, {"ok": True}))
        # the code goes as chat does (follow-up 3): on load-buffer's stdin, where tmux's
        # argument parser never sees it, then pasted, then Enter
        buf = self.env.tmux_loaded()[-1][0]
        self.assertRegex(buf, r"^metachat-[0-9a-f]{8}$")
        self.assertEqual(self.sent(), [["load-buffer", "-b", buf, "-"],
                                       ["paste-buffer", "-d", "-p", "-b", buf, "-t", f"=fc-{n}:"],
                                       ["send-keys", "-t", f"=fc-{n}:", "Enter"]])
        self.assertEqual(self.env.tmux_loaded()[-1], (buf, code.encode()))
        self.assertEqual(self.env.tmux_pasted()[-1], (buf, f"=fc-{n}:", True, code.encode()))
        self.env.clear_calls()
        for bad in ("", "a" * 513, "a\nb", "a\tb", "a\x1bb", "a\x9bb", "a\x00b", "a\ud800b", 5, None):
            s, j = self.keys(name=n, text=bad) if bad is not None else self.env.post(
                "/api/keys", {"name": n, "text": None})
            if bad is None:
                self.assertEqual((s, j), (400, {"ok": False, "error": "send one of key, keys or text"}))
            else:
                self.assertEqual((s, j), (400, {"ok": False, "error": "bad text"}), repr(bad))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.keys(name=n, text="a" * 512)[0], 200)
        # what tmux's argument parser would bend (a trailing ";" ends a command and is
        # dropped, "\;" is its escape) arrives byte for byte on stdin
        for text in ("abc;", "ab\\;", "a;b", "q\\", "-x;", "a\\;b", ";", "é-ü;"):
            self.env.clear_calls()
            self.assertEqual(self.keys(name=n, text=text), (200, {"ok": True}), text)
            self.assertEqual(self.env.tmux_loaded()[-1][1], text.encode("utf-8"), text)
            self.assertEqual(self.env.tmux_pasted()[-1][3], text.encode("utf-8"), text)
            self.assertNotIn("-l", [w for c in self.sent() for w in c], text)
        # never logged: not in ws.log, not in the history
        self.assertNotIn(code, (self.env.state / "ws.log").read_text())
        s, j = self.env.get("/api/events?limit=50")
        self.assertNotIn(code, json.dumps(j))

    def test_the_code_step_is_read_fresh(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        self.assertEqual(self.show(n, screen("claude-code"))["signin"]["step"], "code")   # cached now
        self.env.set_pane(f"fc-{n}", "Login successful. Press Enter to continue…\n")
        self.env.clear_calls()
        self.assertEqual(self.keys(name=n, text="late-code"), (409, {"ok": False, "error": "not at the code step"}))
        self.assertEqual(self.sent(), [])


class KeysDefaultDeskTests(SignInBase):
    config = {"default_desk": "signin-claude-work"}

    def test_text_never_goes_to_the_default_desk(self):
        self.assertEqual(self.signin()[0], 200)
        self.env.set_pane("fc-signin-claude-work", screen("claude-code"))
        self.env.clear_calls()
        s, j = self.env.post("/api/keys", {"name": "signin-claude-work", "text": "code"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "default desk"}))
        self.assertEqual(self.sent(), [])
        s, j = self.env.post("/api/keys", {"name": "signin-claude-work", "key": "enter"})
        self.assertEqual((s, j), (200, {"ok": True}))     # a key may answer the default desk


class DefaultFolderTests(unittest.TestCase):
    """B4: claude and cursor desks open in a project folder, never ~ by default."""

    def spawn_cwd(self, env, kind):
        s, j = env.post("/api/spawn", {"kind": kind})
        self.assertEqual(s, 200, j)
        call = [c for c in env.calls("tmux") if c[:1] == ["new-session"]][-1]
        reg = json.loads((env.state / "desks" / f"{j['name']}.json").read_text())
        self.assertEqual(call[5], reg["cwd"])
        return reg["cwd"]

    def test_first_project_path_then_shell_keeps_home(self):
        with Env() as env:
            alpha = str((env.projects / "alpha").resolve())
            self.assertEqual(self.spawn_cwd(env, "claude"), alpha)
            self.assertEqual(self.spawn_cwd(env, "cursor"), alpha)
            self.assertEqual(self.spawn_cwd(env, "shell"), str(env.home))
            s, j = env.get("/api/meta")
            self.assertEqual(j["default_folder"], alpha)
            beta = str(env.projects / "beta")
            s, j = env.post("/api/spawn", {"kind": "claude", "cwd": beta})
            self.assertEqual(json.loads((env.state / "desks" / f"{j['name']}.json").read_text())["cwd"], beta)

    def test_config_default_folder_wins_when_it_passes(self):
        with Env() as env:
            cfg = json.loads(env.config.read_text())
            cfg["default_folder"] = str(env.projects / "beta")
            env.config.write_text(json.dumps(cfg))
            server.configure()
            beta = str((env.projects / "beta").resolve())
            self.assertEqual(self.spawn_cwd(env, "claude"), beta)
            self.assertEqual(env.get("/api/meta")[1]["default_folder"], beta)
            # a config folder that fails the rules is passed over: the last folder used
            # (beta, the claude desk above; amendment 11-09), and without one the first project
            for bad in ("/tmp", str(env.home), "relative/path", str(env.projects / "missing")):
                cfg["default_folder"] = bad
                env.config.write_text(json.dumps(cfg))
                server.configure()
                self.assertEqual(env.get("/api/meta")[1]["default_folder"], beta, bad)
                (env.state / "last-folder").unlink()
                self.assertEqual(env.get("/api/meta")[1]["default_folder"],
                                 str((env.projects / "alpha").resolve()), bad)
                (env.state / "last-folder").write_text(beta + "\n")

    def test_the_first_project_path_that_passes(self):
        with Env() as env:
            cfg = json.loads(env.config.read_text())
            cfg["projects"] = [{"name": "home", "paths": [str(env.home)]},
                               {"name": "two", "paths": ["/nonexistent", str(env.projects / "beta")]}]
            env.config.write_text(json.dumps(cfg))
            server.configure()
            self.assertEqual(self.spawn_cwd(env, "cursor"), str((env.projects / "beta").resolve()))

    def test_no_folder_is_a_400(self):
        with Env({"projects": [], "default_folder": ""}) as env:
            s, j = env.post("/api/spawn", {"kind": "claude"})
            self.assertEqual((s, j), (400, {"ok": False, "error": "choose a project folder"}))
            s, j = env.post("/api/spawn", {"kind": "cursor"})
            self.assertEqual((s, j), (400, {"ok": False, "error": "choose a project folder"}))
            self.assertIsNone(env.get("/api/meta")[1]["default_folder"])
            self.assertEqual(env.post("/api/spawn", {"kind": "shell"})[0], 200)
            self.assertEqual([c for c in env.calls("tmux") if c[:1] == ["new-session"]][-1][5], str(env.home))


class NeedsYouTests(SignInBase):
    """B5: NEEDS_RE gains the sign-in screens and keeps every pattern it had."""

    def needs(self, name, text):
        return self.show(name, text)["needs_you"]

    def test_the_pattern_gains_the_sign_in_phrases(self):
        for phrase in ("Select login method:", "Paste code here if prompted >", "Press Enter to continue…",
                       "Choose the text style that looks best with your terminal",
                       "Waiting for browser authentication..."):
            self.assertTrue(mb.NEEDS_RE.search(phrase), phrase)

    def test_the_screens_on_a_plain_desk(self):
        self.assertEqual(self.env.post("/api/spawn", {"kind": "claude", "label": "plain"})[0], 200)
        for fixture in ("claude-method", "claude-code", "claude-trust", "claude-tool", "metabot-install",
                        "sudo", "cursor-run", "cursor-login"):
            self.assertTrue(self.needs("plain", "\n".join(["old line"] * 30) + "\n" + screen(fixture)),
                            fixture)
        self.assertTrue(self.needs("plain", "Security notes:\n Press Enter to continue…\n"))   # pattern check
        self.assertFalse(self.needs("plain", screen("claude-method") + "user@host:~/proj$ "))
        self.assertFalse(self.needs("plain", screen("cursor-status")))
        self.assertFalse(self.needs("plain", ""))

    def test_every_waiting_sign_in_screen_needs_the_person(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        for fixture in ("claude-style", "claude-method", "claude-code", "claude-trust"):
            self.assertTrue(self.needs(n, screen(fixture)), fixture)
        self.assertTrue(self.needs(n, "Login successful. Press Enter to continue…\n"))
        self.assertFalse(self.needs(n, ""))
        os.mkfifo(self.work / ".credentials.json")
        self.assertFalse(self.needs(n, ""))                     # ready: nothing waits
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(self.signin(kind="cursor", account="alt")[0], 200)
        self.assertFalse(self.needs("signin-cursor-alt", screen("cursor-status")))
        self.assertTrue(self.needs("signin-cursor-alt", screen("cursor-login")))
        s, j = self.env.get("/api/tree")
        self.assertIn("signin-cursor-alt", [w["desk"] for w in j["waiting"]])
        self.assertNotIn(n, [w["desk"] for w in j["waiting"]])
        # a sign-in screen above a bare shell (the CLI exited): nothing waits
        self.assertFalse(self.needs("signin-cursor-alt", screen("cursor-login") + "user@host:/x$ "))


class AskTests(SignInBase):
    """Follow-ups A and B: a waiting desk row says what kind of answer it wants
    (``ask``: signin > secret > yes > line > select) and which line asks
    (``ask_line``), and carries its class chip (``cls``); all three are null when
    nothing waits. The screens are the fixtures (MetaBot's two asks are its
    approvals.py prompt word for word); a line written inline is a pattern check."""

    OLD = "\n".join(["old line"] * 30) + "\n"

    def setUp(self):
        super().setUp()
        self.assertEqual(self.env.post("/api/spawn", {"kind": "claude", "label": "plain"})[0], 200)

    def ask(self, text, name="plain"):
        d = self.show(name, text)
        return d["needs_you"], d["ask"], d["ask_line"], d["cls"]

    def test_each_screen_names_its_ask(self):
        want = {
            "sudo": ("secret", "[sudo] password for user:", "write"),
            "metabot-ask-destructive": ("yes", "allow? type 'yes' to allow, anything else declines:",
                                        "destructive"),
            "metabot-ask-write": ("line", "allow? type 'y' to allow, anything else declines:", "write"),
            "metabot-install": ("line", "install it? [y/N]", "write"),
            "claude-tool": ("select", "❯ 1. Yes", "destructive"),
            "claude-trust": ("select", "❯ 1. Yes, I trust this folder", "read"),
            "claude-method": ("select", "❯ 1. Claude account with subscription · Individual, Team, or Enterprise",
                              "read"),
            # a waiting prompt no class names reads as a menu; its line is the one that waits
            "cursor-run": ("select", "Run this command?", "write"),
            "claude-code": ("select", "Paste code here if prompted >", "read"),
            "cursor-login": ("select", "Waiting for browser authentication...", "read"),
        }
        for fixture, (ask, line, cls) in want.items():
            self.assertEqual(self.ask(self.OLD + screen(fixture)), (True, ask, line, cls), fixture)

    def test_nothing_waits_and_all_three_are_null(self):
        for text in ("", self.OLD, screen("claude-method") + "user@host:~/proj$ ", screen("cursor-status")):
            self.assertEqual(self.ask(text), (False, None, None, None), text[-40:])

    def test_precedence_and_the_ask_line(self):
        # pattern checks: secret over a [y/N] above it (MetaBot's destructive ask is yes over line)
        self.assertEqual(self.ask("install it? [y/N] y\n[sudo] password for user: ")[:3],
                         (True, "secret", "[sudo] password for user:"))
        # the ask is read off its own line: an older ask still on the screen loses to the newer one
        # (sudo's prompt, answered, above apt's question; MetaBot's destructive ask above a write ask)
        self.assertEqual(self.ask("[sudo] password for user: \nReading package lists... Done\n"
                                  "Do you want to continue? [Y/n] ")[:3],
                         (True, "line", "Do you want to continue? [Y/n]"))
        self.assertEqual(self.ask(screen("metabot-ask-destructive") + "yes\n" + screen("metabot-ask-write"))[:3],
                         (True, "line", "allow? type 'y' to allow, anything else declines:"))
        # the line: control characters out, at most 200 characters
        self.assertEqual(self.ask("Do you want to proceed?\t" + "x" * 300 + "\n")[:3],
                         (True, "select", ("Do you want to proceed?" + "x" * 300)[:200]))
        # a line that holds a link is never the ask line: the query string stays in the pane
        self.assertEqual(self.ask("Allow a fetch of https://example.test/x?k=v\n")[:3], (True, "select", None))

    def test_a_secret_prompt_on_the_last_line_waits(self):
        # pattern checks: a password, a passphrase, a PIN on the line the cursor waits on
        for line in ("Password:", "Enter passphrase for key '/work/u/.ssh/id_ed25519':",
                     "Enter PIN for 'token':", "u@db's password:"):
            self.assertEqual(self.ask(self.OLD + line + " ")[:3], (True, "secret", line), line)
        # the word in output above is no prompt: nothing waits
        self.assertEqual(self.ask("the db password: is in the vault\nbuild finished\n")[:2], (False, None))

    def test_a_menu_waits(self):
        # pattern checks: an option line under the cursor, "Enter to confirm"
        self.assertEqual(self.ask("Pick one\n❯ 1. Alpha\n  2. Beta\n")[:3], (True, "select", "❯ 1. Alpha"))
        self.assertEqual(self.ask("Model\n  Sonnet\nEnter to confirm · Esc to exit\n")[:3],
                         (True, "select", "Enter to confirm · Esc to exit"))

    def test_a_sign_in_desk_asks_signin(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        self.assertEqual(self.ask(screen("claude-code"), n)[:3], (True, "signin", "Paste code here if prompted >"))
        self.assertEqual(self.ask(screen("claude-trust"), n)[:3], (True, "signin", "❯ 1. Yes, I trust this folder"))
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(self.signin(kind="cursor", account="alt")[0], 200)
        # the link's own line matches the step too; the ask line is never the link
        self.assertEqual(self.ask(screen("cursor-login"), "signin-cursor-alt")[:3],
                         (True, "signin", "Waiting for browser authentication..."))

    def test_tree_waiting_rows_carry_ask(self):
        self.show("plain", self.OLD + screen("sudo"))
        s, j = self.env.get("/api/tree")
        w = next(w for w in j["waiting"] if w["desk"] == "plain")
        self.assertEqual((w["ask"], w["ask_line"], w["cls"]), ("secret", "[sudo] password for user:", "write"))


class SignInEndedTests(SignInBase):
    """Follow-up C: a sign-in whose CLI ended (``[metachat] exit N`` is the latest
    match on its screen): step ``ended``, ``exit`` N, no link, nothing waits."""

    def test_a_cursor_login_that_ends_after_the_link(self):
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(self.signin(kind="cursor", account="alt")[0], 200)
        n = "signin-cursor-alt"
        d = self.show(n, screen("cursor-login"))
        self.assertEqual((d["signin"]["step"], d["signin"]["exit"]), ("link", None))
        for tail in ("\n[metachat] exit 1\n", "\n[metachat] exit 1\nuser@host:~/x$ "):
            d = self.show(n, screen("cursor-login") + tail)
            self.assertEqual(d["signin"], {"engine": "cursor", "account": "alt", "step": "ended", "url": None,
                                           "exit": 1}, tail)
            self.assertEqual((d["needs_you"], d["ask"], d["ask_line"], d["cls"]), (False, None, None, None))
        s, j = self.env.get("/api/tree")
        self.assertNotIn(n, [w["desk"] for w in j["waiting"]])

    def test_a_claude_sign_in_that_ends(self):
        self.assertEqual(self.signin()[0], 200)
        n = "signin-claude-work"
        d = self.show(n, screen("claude-code") + "\n[metachat] exit 130\n")
        self.assertEqual(d["signin"], {"engine": "claude", "account": "work", "step": "ended", "url": None,
                                       "exit": 130})
        self.assertFalse(d["needs_you"])
        # the latest match wins: a CLI started again under the exit line shows its own step
        d = self.show(n, "[metachat] exit 1\nuser@host:~$ claude\n" + screen("claude-method"))
        self.assertEqual((d["signin"]["step"], d["signin"]["exit"]), ("method", None))
        self.assertTrue(d["needs_you"])


if __name__ == "__main__":
    unittest.main()
