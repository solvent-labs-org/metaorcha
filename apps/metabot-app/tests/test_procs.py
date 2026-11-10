"""A4: one process table from ``ps`` for hosts without /proc (macOS), used by the
parent walk that finds a run's desk and by the reply mirror's process lookup."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import mb, procs  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "ps"
SID = "0f3b2c4e-1111-4222-8333-944455556666"


class ParseTests(unittest.TestCase):
    def test_linux_sample(self):
        t = procs.parse((FIX / "linux.txt").read_text())
        self.assertEqual(t[955], (901, f"claude --resume {SID}"))
        self.assertEqual(t[1], (0, "/sbin/init splash"))
        self.assertEqual(t[1400][0], 955)

    def test_macos_sample(self):
        t = procs.parse((FIX / "macos.txt").read_text())
        self.assertEqual(t[455], (402, f"/Users/u/.local/bin/claude --session-id {SID}"))
        self.assertEqual(t[700], (455, "cursor-agent --use-system-ca"))

    def test_header_and_junk_lines_are_skipped(self):
        t = procs.parse("  PID  PPID ARGS\n  12  1 sleep 9\nnot a row\n\n")
        self.assertEqual(t, {12: (1, "sleep 9")})

    def test_children_index(self):
        t = procs.parse((FIX / "linux.txt").read_text())
        self.assertEqual(sorted(procs.children(t)[1]), [812, 1203])
        self.assertEqual(procs.children(t)[955], [1400])


class NoProcTests(unittest.TestCase):
    """/proc hidden: every lookup falls back to the table."""

    def setUp(self):
        self.table = procs.parse((FIX / "linux.txt").read_text())
        for p in (mock.patch.object(procs, "PROC", Path("/nonexistent-proc-for-tests")),
                  mock.patch.object(procs, "table", lambda: dict(self.table))):
            p.start(); self.addCleanup(p.stop)

    def test_ppid_from_the_table(self):
        self.assertEqual(procs.ppid(955), 901)
        self.assertEqual(procs.ppid(1), 0)
        self.assertIsNone(procs.ppid(424242))

    def test_argv_from_the_table(self):
        self.assertEqual(procs.argv(955), ["claude", "--resume", SID])
        self.assertIsNone(procs.argv(424242))

    def test_desk_of_pid_walks_the_table(self):
        env = Env().start(); self.addCleanup(env.stop)
        with mock.patch.object(mb, "pane_pids", lambda ctx: {"deskA": 901}), \
                mock.patch.object(mb, "registry", lambda ctx: {"deskA": {"kind": "claude", "cwd": "/w/a"}}):
            self.assertEqual(mb.desk_of_pid(server.MB, 1400), "deskA")   # node -> claude -> bash(pane)
            self.assertIsNone(mb.desk_of_pid(server.MB, 1300))           # the other tree

    def test_proc_claude_falls_back_to_table_pane_cwd_and_registry(self):
        env = Env().start(); self.addCleanup(env.stop)
        server._pane_cwd.clear(); server._pane_cwd["deskA"] = "/w/a"
        reg = {"deskA": {"kind": "claude", "cwd": "/w/a", "env_add": {"CLAUDE_CONFIG_DIR": "/h/.claude-x"}}}
        with mock.patch.object(mb, "registry", lambda ctx: reg):
            self.assertEqual(server._proc_claude(955, desk="deskA"),
                             (["claude", "--resume", SID], "/w/a", "/h/.claude-x"))
            self.assertIsNone(server._proc_claude(424242, desk="deskA"))


class WithProcTests(unittest.TestCase):
    def test_own_process_reads_proc_when_present(self):
        if not Path("/proc/self/stat").exists():
            self.skipTest("no /proc here")
        self.assertEqual(procs.ppid(os.getpid()), os.getppid())
        self.assertTrue(procs.argv(os.getpid()))


if __name__ == "__main__":
    unittest.main()
