"""Clean testing (2026-11-09): a stopped app leaves no reader process behind and the
test client leaves no socket open."""
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env  # noqa: E402
from metabot_app import mb  # noqa: E402


class CleanTeardownTests(unittest.TestCase):
    def test_close_reader_stops_the_warm_worker(self):
        env = Env().start()
        try:
            env.get("/api/accounts")          # starts the warm MetaBot reader
        finally:
            env.stop()
        self.assertIsNone(mb._MBQW["worker"])
        self.assertIsNone(mb._MBQW["key"])

    def test_close_reader_twice_is_harmless(self):
        mb.close_reader()
        mb.close_reader()

    def test_the_client_closes_its_socket(self):
        env = Env().start()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                env.get("/api/meta")
                import gc
                gc.collect()
            leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual(leaks, [], [str(w.message) for w in leaks])
        finally:
            env.stop()


if __name__ == "__main__":
    unittest.main()


class TerminalChildReapedTests(unittest.TestCase):
    """A closed terminal must not leave its attach process behind as a zombie."""

    def _children(self):
        import os
        import subprocess
        out = subprocess.run(["ps", "-o", "pid=,stat=,args=", "--ppid", str(os.getpid())],
                             capture_output=True, text=True).stdout
        return [ln.split(None, 2) for ln in out.splitlines() if ln.strip() and "ps -o" not in ln]

    def test_closing_a_terminal_waits_for_its_child(self):
        import gc
        import time
        import warnings
        from harness import ws_read_frame
        env = Env().start()
        try:
            env.add_session("fc-reap")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                s, status, rest = env.ws("/ws/term?desk=reap")
                self.assertEqual(status, 101)
                ws_read_frame(s, rest)
                s.close()
                time.sleep(1.0)        # the server notices the close and tears the session down
                gc.collect()
            left = [str(w.message) for w in caught
                    if issubclass(w.category, ResourceWarning) and "subprocess" in str(w.message)]
            self.assertEqual(left, [])
        finally:
            env.stop()
