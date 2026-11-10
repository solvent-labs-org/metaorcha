"""Review round on 1.2 (11-09): the fixes for the two reviewers' blockers.

- the ask class is read off the whole captured tail, not its last 6 lines: a
  Claude Code 2.1.319 Bash dialog (three options and a footer) puts the
  command 7 non-empty lines up. claude-bash-2.1.319.txt is a pattern check
  built from the dialog's strings in the installed binary, not a captured screen;
- the ask line skips hint-only lines (Esc to cancel, Tab to amend, Enter to confirm);
- keys answer only a desk that waits now (read fresh), and only the ask the
  tablet showed when it names one; after a send the desk's cached read is dropped;
- a waiting row whose pane read names no ask carries no ask and no class;
- every tmux argument that ends in ";" is escaped, so tmux never splits a
  command there (tmux 3.4 ends a command at such an argument, even after --)."""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server  # noqa: E402
from metabot_app import mb, tmuxcmd  # noqa: E402

SCREENS = FIX / "screens"
OLD = "\n".join(["old line"] * 30) + "\n"


def screen(name):
    return (SCREENS / f"{name}.txt").read_text(encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.assertEqual(self.env.post("/api/spawn", {"kind": "claude", "label": "plain"})[0], 200)

    def tearDown(self):
        self.env.stop()

    def row(self, name="plain", clear=True):
        if clear:
            server.MB._needs.clear()
            server.invalidate()
        s, j = self.env.get("/api/desks")
        self.assertEqual(s, 200)
        return next(d for d in j["desks"] if d["name"] == name)

    def sent(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["send-keys"]]


class ClassWindowTests(Base):
    def test_the_command_above_six_lines_still_classes_the_ask(self):
        self.env.set_pane("fc-plain", OLD + screen("claude-bash-2.1.319"))
        d = self.row()
        self.assertEqual((d["needs_you"], d["ask"], d["cls"]), (True, "select", "destructive"))

    def test_three_options_and_a_two_line_description(self):
        text = (" Bash command\n   git push origin main --force\n   Push the rewritten history\n"
                "   to the remote main branch\n Do you want to proceed?\n ❯ 1. Yes\n"
                "   2. Yes, and don't ask again\n   3. No (esc)\n")
        self.env.set_pane("fc-plain", OLD + text)
        self.assertEqual(self.row()["cls"], "destructive")

    def test_the_ask_line_is_never_a_hint(self):
        self.env.set_pane("fc-plain", OLD + screen("claude-bash-2.1.319"))
        self.assertEqual(self.row()["ask_line"], "❯ 1. Yes")
        self.env.set_pane("fc-plain", OLD + screen("claude-trust"))
        self.assertNotRegex(self.row()["ask_line"], r"(?i)^enter to confirm|^esc to cancel")
        # a hint that is the only asking line stays the line
        self.env.set_pane("fc-plain", "Model\n  Sonnet\nEnter to confirm · Esc to exit\n")
        self.assertEqual(self.row()["ask_line"], "Enter to confirm · Esc to exit")


class KeysFreshTests(Base):
    def keys(self, **body):
        return self.env.post("/api/keys", {"name": "plain", **body})

    def test_keys_need_a_desk_that_waits_now(self):
        self.env.set_pane("fc-plain", "user@host:~/proj$ ")
        self.env.clear_calls()
        self.assertEqual(self.keys(keys=["up", "enter"]), (409, {"ok": False, "error": "nothing waits"}))
        self.assertEqual(self.sent(), [])

    def test_a_stale_cached_ask_is_not_answered(self):
        self.env.set_pane("fc-plain", OLD + screen("claude-trust"))
        self.assertTrue(self.row()["needs_you"])          # cached as waiting
        self.env.set_pane("fc-plain", "user@host:~/proj$ ")
        self.env.clear_calls()
        self.assertEqual(self.keys(key="enter")[0], 409)
        self.assertEqual(self.sent(), [])

    def test_the_ask_the_tablet_showed_must_still_be_the_ask(self):
        self.env.set_pane("fc-plain", OLD + screen("claude-bash-2.1.319"))
        line = self.row()["ask_line"]
        self.env.clear_calls()
        self.assertEqual(self.keys(key="esc", ask_line="❯ 1. Something else"),
                         (409, {"ok": False, "error": "the ask changed"}))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.keys(key="esc", ask_line=line), (200, {"ok": True}))
        for bad in (5, "x" * 201, ["a"]):
            self.assertEqual(self.keys(key="esc", ask_line=bad), (400, {"ok": False, "error": "bad ask_line"}))

    def test_after_a_send_the_row_reads_the_pane_again(self):
        self.env.set_pane("fc-plain", OLD + screen("claude-trust"))
        self.assertTrue(self.row()["needs_you"])
        self.assertEqual(self.keys(key="enter")[0], 200)
        self.env.set_pane("fc-plain", "Claude is working…\n")
        self.assertFalse(self.row(clear=False)["needs_you"])    # no 2 s cache, no 1 s snapshot


class WaitingRowTests(unittest.TestCase):
    def test_no_ask_read_means_no_ask_and_no_class(self):
        w = server._waiting_row("d", {"needs": False})
        self.assertEqual((w["ask"], w["cls"]), (None, None))


@unittest.skipUnless(shutil.which("tmux"), "needs the tmux binary")
class TmuxSemicolonTests(unittest.TestCase):
    """A real tmux on a private socket in a temp dir: never the user's server."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mc-semi-"))
        sock = self.dir / "sock"
        wrap = self.dir / "tmux"
        wrap.write_text(f'#!/bin/sh\nexec {shutil.which("tmux")} -S {sock} "$@"\n')
        wrap.chmod(0o700)
        self.wrap = str(wrap)
        self.ctx = types.SimpleNamespace()
        # A5: mb.tmux builds its command through tmuxcmd: the wrapper is the ``tmux``
        # on PATH and no -L is added (it would override the wrapper's -S).
        self._env = mock.patch.dict(os.environ, {"PATH": f"{self.dir}:{os.environ.get('PATH', '')}"})
        self._env.start()
        self._sock = tmuxcmd.socket()
        tmuxcmd.configure("")
        subprocess.run([self.wrap, "new-session", "-d", "-s", "probe", "--", "sleep", "60"], check=True)

    def tearDown(self):
        subprocess.run([self.wrap, "kill-server"], capture_output=True)
        tmuxcmd.configure(self._sock)
        self._env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_trailing_semicolon_never_starts_a_second_command(self):
        mark = self.dir / "pwned"
        mb.tmux(self.ctx, "set-environment", "-g", "V", "x;", "run-shell", f"touch {mark}")
        self.assertFalse(mark.exists())

    def test_values_arrive_byte_for_byte(self):
        for v in ("x;", "y\;", ";", "a;;", "b\;", "plain"):
            rc, _, err = mb.tmux(self.ctx, "set-environment", "-g", "V", v)
            self.assertEqual(rc, 0, err)
            self.assertEqual(mb.tmux(self.ctx, "show-environment", "-g", "V")[1].strip(), f"V={v}", v)


if __name__ == "__main__":
    unittest.main()
