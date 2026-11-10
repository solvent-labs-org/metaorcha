"""Resume (11-09): a Claude session that ended (its name is in the app's names
folder, <state>/names, or an inbox adapter's; its transcript is in one of the
``claude_homes`` folders; no CLI runs for it) reopens in a desk with
``claude --resume <id>`` in its own folder, so chat reaches it again."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import ECHO_INBOX, Env, server  # noqa: E402

SID = "0f0e0d0c-1111-2222-3333-444455556666"
SID2 = "a0a0a0a0-1111-2222-3333-444455556666"


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.env = Env({"claude_homes": [".claude", ".claude-work"]}).start()
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        self.alpha = str(self.env.projects / "alpha")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.env.stop()

    def saved(self, sid, name="old-chat", cwd=None, age=3600, base=".claude"):
        if name:
            (server.NAMES_DIR / sid).write_text(name + "\n")
        d = self.env.home / base / "projects/-p"
        d.mkdir(parents=True, exist_ok=True)
        t = d / f"{sid}.jsonl"
        t.write_text(json.dumps({"type": "summary"}) + "\n"
                     + json.dumps({"type": "user", "cwd": cwd or self.alpha}) + "\n")
        os.utime(t, (time.time() - age, time.time() - age))
        server._sess_cache[0] = 0.0
        server.invalidate()

    def resume(self, name="old-chat"):
        self.env.clear_calls()
        return self.env.post("/api/pane", {"target": name, "action": "resume"})

    def news(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]

    def keys(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["send-keys"]]

    def pane(self, argv0):
        p = subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 60"])
        self.procs.append(p)
        time.sleep(0.2)
        self.env.add_session("fc-old-chat", pid=p.pid, cwd=self.alpha)
        server.invalidate()

    def test_an_ended_session_opens_in_a_new_desk_with_its_id(self):
        self.saved(SID)
        s, j = self.resume()
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "name": "old-chat"}))
        (cmd,) = self.news()
        self.assertEqual(cmd[cmd.index("-s") + 1], "fc-old-chat")
        self.assertEqual(cmd[cmd.index("-c") + 1], self.alpha)
        self.assertEqual(cmd[-3:], ["claude", "--resume", SID])

    def test_the_newest_transcript_under_the_name_wins(self):
        self.saved(SID, age=7200)
        self.saved(SID2, age=3600)
        s, j = self.resume()
        self.assertEqual(s, 200, j)
        self.assertEqual(self.news()[0][-1], SID2)

    def test_a_session_written_moments_ago_is_not_opened_twice(self):
        self.saved(SID, age=5)
        s, j = self.resume()
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertIn("another terminal", j["error"])
        self.assertEqual(self.news(), [])

    def test_no_saved_session_is_said(self):
        s, j = self.resume("nobody")
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertIn("no saved Claude session", j["error"])

    def test_a_folder_outside_the_project_roots_is_refused(self):
        self.saved(SID, cwd=str(self.env.home))
        s, j = self.resume()
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertIn("folder", j["error"])
        self.assertEqual(self.news(), [])

    def test_an_idle_desk_resumes_in_its_own_terminal(self):
        self.saved(SID)
        self.pane("sleep")   # a bare shell: the desk's claude exited
        s, j = self.resume()
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "name": "old-chat"}))
        self.assertEqual(self.news(), [])
        typed = self.keys()
        self.assertEqual(typed[0][:4], ["send-keys", "-t", "=fc-old-chat:", "-l"])
        self.assertEqual(typed[0][4], f"cd -- {self.alpha} && claude --resume {SID}")
        self.assertEqual(typed[1], ["send-keys", "-t", "=fc-old-chat:", "Enter"])

    def test_a_desk_where_claude_runs_is_not_resumed(self):
        self.saved(SID)
        self.pane("claude")
        s, j = self.resume()
        self.assertEqual((s, j["ok"]), (409, False))
        self.assertIn("already runs", j["error"])
        self.assertEqual(self.keys(), [])

    def test_a_name_tmux_cannot_carry_gets_a_desk_with_its_alias(self):
        self.saved(SID, name="Old-Chat")
        s, j = self.resume("Old-Chat")
        self.assertEqual((s, j["ok"]), (200, True), j)
        self.assertEqual(j["name"], "claude")
        self.assertEqual(server.load_aliases().get("claude"), "Old-Chat")

    def test_a_session_of_the_second_config_folder_resumes_with_it(self):
        conv = str(self.env.home / ".claude-work")
        self.saved(SID, base=".claude-work")
        s, j = self.resume()
        self.assertEqual(s, 200, j)
        (cmd,) = self.news()
        self.assertIn(f"CLAUDE_CONFIG_DIR={conv}", cmd)
        self.assertEqual(cmd[-3:], ["claude", "--resume", SID])

    def test_an_idle_desk_of_the_second_config_folder_types_it(self):
        conv = str(self.env.home / ".claude-work")
        self.saved(SID, base=".claude-work")
        self.pane("sleep")
        s, j = self.resume()
        self.assertEqual(s, 200, j)
        self.assertEqual(self.keys()[0][4], f"cd -- {self.alpha} && CLAUDE_CONFIG_DIR={conv} claude --resume {SID}")

    def test_the_names_live_in_the_state_folder(self):
        self.assertEqual(server.NAMES_DIR, self.env.state / "names")
        self.saved(SID)
        self.assertTrue((self.env.state / "names" / SID).is_file())
        s, j = self.resume()
        self.assertEqual((s, j["ok"]), (200, True), j)
        self.assertEqual(self.news()[0][-3:], ["claude", "--resume", SID])

    def test_a_config_folder_not_in_claude_homes_is_never_read(self):
        self.saved(SID, base=".claude-other")
        s, j = self.resume()
        self.assertEqual((s, j), (409, {"ok": False, "error": "no saved Claude session under this name"}))
        self.assertEqual(self.news(), [])

    def test_claude_homes_keeps_folder_names_under_home_only(self):
        cfg = json.loads(self.env.config.read_text())
        cfg["claude_homes"] = ["/etc", "../up", "", "~/.claude-x", 7, ".claude-work"]
        self.env.config.write_text(json.dumps(cfg))
        server.configure()
        self.assertEqual(server.CLAUDE_HOMES, (".claude-work",))
        cfg["claude_homes"] = ["/etc"]
        self.env.config.write_text(json.dumps(cfg))
        server.configure()
        self.assertEqual(server.CLAUDE_HOMES, (".claude",))
        self.assertEqual(server.DEFAULTS["claude_homes"], [".claude"])

    def test_an_inbox_adapters_names_folder_is_read_too(self):
        cfg = json.loads(self.env.config.read_text())
        cfg["inbox_module"] = str(ECHO_INBOX)
        self.env.config.write_text(json.dumps(cfg))
        server.configure()
        theirs = self.env.state / "echo-inbox" / "names"
        theirs.mkdir(parents=True)
        self.saved(SID, name=None)
        (theirs / SID).write_text("old-chat\n")
        server._sess_cache[0] = 0.0
        server.invalidate()
        self.assertIn("old-chat", server.sessions())
        s, j = self.resume()
        self.assertEqual((s, j["ok"]), (200, True), j)
        self.assertEqual(self.news()[0][-3:], ["claude", "--resume", SID])

    def test_a_desk_the_app_started_resumes_by_its_recorded_id(self):
        s, j = self.env.post("/api/spawn", {"kind": "claude", "cwd": self.alpha, "label": "app-desk"})
        self.assertEqual(s, 200, j)
        sid = json.loads((self.env.state / "desks" / "app-desk.json").read_text())["session_id"]
        self.saved(sid, name=None)   # Claude wrote its transcript; no names file names it
        s, j = self.env.get("/api/desks")
        row = next(d for d in j["desks"] if d["name"] == "app-desk")
        self.assertEqual((row["reach"], row["resume"]), ("idle", True))   # claude exited, a bare shell is left
        s, j = self.resume("app-desk")
        self.assertEqual((s, j), (200, {"ok": True, "error": "", "name": "app-desk"}))
        self.assertEqual(self.keys()[0][4], f"cd -- {self.alpha} && claude --resume {sid}")

    def test_start_claude_then_exit_then_resume_uses_the_same_session(self):
        self.pane("sleep")
        (server.NAMES_DIR / "old-chat").write_text("old-chat\n")
        server._sess_cache[0] = 0.0
        server.invalidate()
        s, j = self.env.post("/api/pane", {"target": "old-chat", "action": "start-claude"})
        self.assertEqual(s, 200, j)
        typed = [c for c in self.env.calls("tmux") if c[:1] == ["send-keys"]]
        sid = typed[0][-1].rsplit(" ", 1)[-1]
        self.saved(sid, name=None)   # Claude ran and exited; its transcript is on disk
        s, j = self.resume()
        self.assertEqual(s, 200, j)
        self.assertEqual(self.keys()[0][4], f"cd -- {self.alpha} && claude --resume {sid}")

    def test_the_desk_row_says_it_can_resume(self):
        self.saved(SID)
        s, j = self.env.get("/api/desks")
        row = next(d for d in j["desks"] if d["name"] == "old-chat")
        self.assertEqual((row["reach"], row["resume"]), ("none", True))


if __name__ == "__main__":
    unittest.main()
