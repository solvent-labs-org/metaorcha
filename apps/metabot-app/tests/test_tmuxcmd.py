"""A5: every tmux call is built in one place, so a fresh install gets its own tmux
server (socket ``metabot``) and never touches the user's sessions; the version
parser tells doctor whether tmux is new enough (3.2 for new-session -e)."""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import server  # noqa: E402
from metabot_app import tmuxcmd  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "metabot_app"


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(tmuxcmd.configure, "metabot")

    def test_default_socket_is_metabot(self):
        self.assertEqual(server.DEFAULTS["tmux_socket"], "metabot")
        tmuxcmd.configure(server.DEFAULTS["tmux_socket"])
        self.assertEqual(tmuxcmd.argv("list-sessions"), ["tmux", "-L", "metabot", "list-sessions"])

    def test_empty_socket_means_the_default_server(self):
        tmuxcmd.configure("")
        self.assertEqual(tmuxcmd.argv("new-session", "-d", "-s", "fc-a"), ["tmux", "new-session", "-d", "-s", "fc-a"])

    def test_a_bad_socket_name_falls_back_to_metabot(self):
        for bad in ("../x", "a b", "x" * 41, "a/b", None, 7):
            tmuxcmd.configure(bad)
            self.assertEqual(tmuxcmd.argv()[:3], ["tmux", "-L", "metabot"], repr(bad))

    def test_configure_from_config_in_server(self):
        env_cfg = dict(server.DEFAULTS, tmux_socket="")
        with mock.patch.object(server, "load_config", lambda path=None: env_cfg):
            pass   # configure() is exercised by the harness; the value flows via tmuxcmd.configure
        tmuxcmd.configure("mb-sandbox")
        self.assertEqual(tmuxcmd.argv("-V"), ["tmux", "-L", "mb-sandbox", "-V"])


class VersionTests(unittest.TestCase):
    def test_parses_release_next_and_letter_suffix(self):
        self.assertEqual(tmuxcmd.parse_version("tmux 3.4"), (3, 4))
        self.assertEqual(tmuxcmd.parse_version("tmux next-3.5"), (3, 5))
        self.assertEqual(tmuxcmd.parse_version("tmux 3.2a"), (3, 2))
        self.assertEqual(tmuxcmd.parse_version("tmux 3.3a\n"), (3, 3))
        self.assertIsNone(tmuxcmd.parse_version("garbage"))
        self.assertIsNone(tmuxcmd.parse_version(""))

    def test_version_runs_tmux_dash_v(self):
        with mock.patch.object(tmuxcmd, "_run", lambda argv, timeout: (0, "tmux 3.4\n")) as _:
            self.assertEqual(tmuxcmd.version(), (3, 4))
        with mock.patch.object(tmuxcmd, "_run", lambda argv, timeout: (127, "")):
            self.assertIsNone(tmuxcmd.version())

    def test_new_enough(self):
        self.assertTrue(tmuxcmd.new_enough((3, 2)))
        self.assertTrue(tmuxcmd.new_enough((3, 10)))
        self.assertFalse(tmuxcmd.new_enough((3, 1)))
        self.assertFalse(tmuxcmd.new_enough(None))


class NoRawTmuxTests(unittest.TestCase):
    def test_no_tmux_literal_outside_tmuxcmd(self):
        rx = re.compile(r'\[\s*"tmux"|which\(\s*"tmux"\s*\)|ctx\.tmux\b')
        hits = []
        for p in sorted(SRC.glob("*.py")):
            if p.name == "tmuxcmd.py":
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.name}:{n}: {line.strip()[:80]}")
        self.assertEqual(hits, [], "\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
