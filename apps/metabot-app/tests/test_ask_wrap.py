"""Found on the tablet 11-09: Claude Code wraps its own hint line with a hard newline
on a narrow pane ("… · Type to search · Esc" / "to cancel"), so an open picker read
as "nothing waits" and the desk showed no needs-you while chat into it was refused."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env  # noqa: E402,F401  (puts the app on sys.path)
from metabot_app import mb  # noqa: E402

PICKER = [
    "  Resume session",
    "  ⌕ some search",
    "   No sessions match \"some search\".",
    "    Ctrl+A to show all projects · Ctrl+B to only show current branch · Space to preview · Ctrl+R to rename · Type to search · Esc",
    "    to cancel",
]


class WrappedAskTests(unittest.TestCase):
    def test_a_hint_wrapped_over_two_lines_still_asks(self):
        asks = mb.asking_lines(PICKER)
        self.assertTrue(asks, "the wrapped 'Esc to cancel' is an ask")
        ask, line = mb.ask_of(asks)
        self.assertEqual(ask, "select")
        self.assertIn("Esc", line)

    def test_a_wrapped_dialog_question_still_asks(self):
        self.assertTrue(mb.asking_lines(["  Do you want", "  to proceed?", " ❯ 1. Yes", "   2. No"]))

    def test_screen_order_holds_a_later_single_line_ask_wins(self):
        asks = mb.asking_lines(["  … · Esc", "  to cancel", "  install it? [y/N]"])
        self.assertEqual(asks[-1], "  install it? [y/N]")

    def test_plain_text_split_over_lines_is_not_an_ask(self):
        self.assertEqual(mb.asking_lines(["I will now", "run the tests", "and report back."]), [])

    def test_a_shell_line_and_a_word_never_join_into_an_ask(self):
        self.assertEqual(mb.asking_lines(["$ echo esc", "to cancel later"]), [])


if __name__ == "__main__":
    unittest.main()


import time  # noqa: E402
import subprocess  # noqa: E402
from harness import server  # noqa: E402


class UnregisteredDeskTests(unittest.TestCase):
    """A tmux desk the app did not make (an older desk, one made by hand)
    waits on a dialog: the row must say so, as paste_pane already refuses chat into it."""

    def setUp(self):
        self.env = Env().start()
        self.p = subprocess.Popen(["bash", "-c", "exec -a claude sleep 60"])
        time.sleep(0.2)

    def tearDown(self):
        self.p.kill()
        self.p.wait()
        self.env.stop()

    def test_a_waiting_desk_outside_the_registry_shows_needs_you(self):
        self.env.add_session("fc-hand-made", pid=self.p.pid, pane="\n".join(PICKER) + "\n")
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "hand-made").write_text("hand-made\n")
        server._sess_cache[0] = 0.0
        server.invalidate()
        server.MB._needs.clear()
        s, j = self.env.get("/api/desks")
        row = next(d for d in j["desks"] if d["name"] == "hand-made")
        self.assertEqual((row["reach"], row["needs_you"], row["ask"]), ("pane", True, "select"))
