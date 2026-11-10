"""Replies in the chat (11-09): a Claude desk the app types into answers in its
terminal; the PC reads that Claude's transcript and posts each new assistant
text block into the desk's lane (mirror_once). Nothing old is replayed; tool
calls, sidechains (subagents) and user lines are not posted; an armed desk
answers through the inbox adapter, so it is not mirrored twice."""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Env, server  # noqa: E402

SID = "1a2b3c4d-1111-4222-8333-444455556666"
SID_B = "5e6f7a8b-1111-4222-8333-444455556666"
IDLE_CLAUDE = "╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts\n"


def line(kind, text=None, side=False, blocks=None):
    if kind == "assistant":
        content = blocks if blocks is not None else [{"type": "text", "text": text}]
        return json.dumps({"type": "assistant", "isSidechain": side, "message": {"role": "assistant", "content": content}}) + "\n"
    return json.dumps({"type": "user", "isSidechain": side, "message": {"role": "user", "content": text}}) + "\n"


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.alpha = str(self.env.projects / "alpha")
        self.procs = []
        server._mirror.clear()

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.env.stop()

    def desk(self, name="mirror-one", args=""):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}   # the default sign-in, as a desk has
        p = subprocess.Popen(["bash", "-c", f"exec -a claude python3 -c 'import time; time.sleep(60)' {args}"],
                             cwd=self.alpha, env=env)
        self.procs.append(p)
        time.sleep(0.3)
        self.env.add_session(f"fc-{name}", pid=p.pid, cwd=self.alpha, pane=IDLE_CLAUDE)
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / name).write_text(name + "\n")
        server._sess_cache[0] = 0.0
        server.invalidate()
        return p

    def transcript(self, sid=SID, age=0):
        d = self.env.home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", self.alpha)
        d.mkdir(parents=True, exist_ok=True)
        t = d / f"{sid}.jsonl"
        if not t.exists():
            t.write_text(line("user", "earlier question") + line("assistant", "old answer"))
        if age:
            os.utime(t, (time.time() - age, time.time() - age))
        return t

    def mirrored(self):
        with server._local_lock:
            return [e for e in server._local if e.get("mirror")]

    def tick(self):
        server.invalidate()
        server.mirror_once()

    def test_a_new_answer_lands_in_the_desk_lane_and_nothing_old_is_replayed(self):
        self.desk()
        t = self.transcript()
        self.tick()
        self.assertEqual(self.mirrored(), [])
        with open(t, "a") as f:
            f.write(line("user", "hello"))
            f.write(line("assistant", blocks=[{"type": "thinking", "thinking": "hm"}, {"type": "tool_use", "name": "Bash", "input": {}}]))
            f.write(line("assistant", "a subagent note", side=True))
            f.write(line("assistant", "  new answer  "))
        self.tick()
        ev = self.mirrored()
        self.assertEqual([(e["message"], e["target"], e["from"], e["via"], e["typed"]) for e in ev],
                         [("new answer", "mirror-one", "mirror-one", "desk", False)])
        self.tick()
        self.assertEqual(len(self.mirrored()), 1)

    def test_a_half_written_line_waits_for_its_end(self):
        self.desk()
        t = self.transcript()
        self.tick()
        whole = line("assistant", "split answer")
        with open(t, "a") as f:
            f.write(whole[:20])
        self.tick()
        self.assertEqual(self.mirrored(), [])
        with open(t, "a") as f:
            f.write(whole[20:])
        self.tick()
        self.assertEqual([e["message"] for e in self.mirrored()], ["split answer"])

    def test_an_armed_desk_is_not_mirrored(self):
        self.desk()
        t = self.transcript()
        self.env.arm("mirror-one")
        self.tick()
        with open(t, "a") as f:
            f.write(line("assistant", "answered through the inbox"))
        self.tick()
        self.assertEqual(self.mirrored(), [])

    def test_the_session_named_on_the_command_line_wins_over_a_newer_file(self):
        self.desk(args=f"--session-id {SID}")
        mine = self.transcript(SID, age=120)
        other = self.transcript(SID_B)          # newer, another Claude in the same folder
        self.tick()
        with open(other, "a") as f:
            f.write(line("assistant", "not this desk"))
        with open(mine, "a") as f:
            f.write(line("assistant", "this desk"))
        self.tick()
        self.assertEqual([e["message"] for e in self.mirrored()], ["this desk"])

    def test_a_desk_on_another_sign_in_reads_that_folder(self):
        conf = self.env.home / "acct"
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(conf))
        p = subprocess.Popen(["bash", "-c", "exec -a claude python3 -c 'import time; time.sleep(60)'"], cwd=self.alpha, env=env)
        self.procs.append(p)
        time.sleep(0.3)
        self.env.add_session("fc-acct-one", pid=p.pid, cwd=self.alpha, pane=IDLE_CLAUDE)
        (server.NAMES_DIR).mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "acct-one").write_text("acct-one\n")
        server._sess_cache[0] = 0.0
        d = conf / "projects" / re.sub(r"[^A-Za-z0-9]", "-", self.alpha)
        d.mkdir(parents=True)
        t = d / f"{SID}.jsonl"
        t.write_text(line("assistant", "old"))
        self.transcript(SID_B)   # the default folder's transcript is not this desk's
        self.tick()
        with open(t, "a") as f:
            f.write(line("assistant", "from the account"))
        self.tick()
        self.assertEqual([e["message"] for e in self.mirrored()], ["from the account"])

    def test_a_long_answer_is_cut_and_says_so(self):
        self.desk()
        t = self.transcript()
        self.tick()
        with open(t, "a") as f:
            f.write(line("assistant", "x" * 20000))
        self.tick()
        (ev,) = self.mirrored()
        self.assertLessEqual(len(ev["message"]), server.MIRROR_TEXT_MAX + 80)
        self.assertIn("full answer in the terminal", ev["message"])


if __name__ == "__main__":
    unittest.main()


class MirrorStreamTests(unittest.TestCase):
    """Found on the tablet 11-09: the live stream sent a fixed field list without
    "mirror", so a reply read "desk → desk" until the page was reloaded."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_the_stream_carries_the_mirror_mark(self):
        from test_hotfix import open_stream, read_event
        s = open_stream(self.env)
        try:
            server.local_event("an answer", "desk-x", "desk-x", via="desk", typed=False, mirror=True)
            ev = read_event(s)
            while ev.get("app"):          # the hello comes first
                ev = read_event(s)
            self.assertEqual((ev["message"], ev.get("mirror")), ("an answer", True))
            server.local_event("a plain one", "desk-x", "", via="in")
            ev = read_event(s)
            self.assertNotIn("mirror", ev)
        finally:
            s.close()
