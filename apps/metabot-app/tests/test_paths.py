"""A2: MetaBot's binaries are found beside the app's own interpreter, then on PATH.
No default names a folder on this developer's machine, and a tilde in the config is
expanded before anything is run (subprocess never expands one)."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import server  # noqa: E402
from metabot_app import mb, paths  # noqa: E402


def _exe(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class DefaultsTests(unittest.TestCase):
    def test_no_default_names_a_home_folder(self):
        self.assertNotIn("/home/", json.dumps(server.DEFAULTS))

    def test_binary_defaults_are_empty(self):
        """Empty means "find it": beside the interpreter, then on PATH."""
        for key in ("metabot_bin", "orcha_bin", "metabot_python"):
            self.assertEqual(server.DEFAULTS[key], "", key)


class FindBinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="paths-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.venv = self.tmp / "venv" / "bin"
        self.onpath = self.tmp / "onpath"
        _exe(self.venv / "python")
        self.beside = _exe(self.venv / "metabot")
        self.path_one = _exe(self.onpath / "metabot")
        p1 = mock.patch.object(sys, "executable", str(self.venv / "python"))
        p2 = mock.patch.dict(os.environ, {"PATH": str(self.onpath), "SHELL": str(self.tmp / "no-shell")})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        paths.login_path.cache_clear()
        self.addCleanup(paths.login_path.cache_clear)

    def test_beside_the_interpreter_wins_over_path(self):
        self.assertEqual(paths.find_bin("metabot"), str(self.beside))

    def test_path_when_nothing_is_beside(self):
        self.beside.unlink()
        self.assertEqual(paths.find_bin("metabot"), str(self.path_one))

    def test_a_file_that_is_not_executable_is_skipped(self):
        self.beside.chmod(0o644)
        self.assertEqual(paths.find_bin("metabot"), str(self.path_one))

    def test_none_when_nowhere(self):
        self.assertIsNone(paths.find_bin("no-such-tool-here"))


class CtxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="paths-ctx-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.venv = self.tmp / "venv" / "bin"
        _exe(self.venv / "python")
        self.beside = _exe(self.venv / "metabot")
        self.orcha = _exe(self.venv / "orcha")
        home = self.tmp / "home"
        home.mkdir()
        self.home_bin = _exe(home / "tools" / "metabot")
        for p in (mock.patch.object(sys, "executable", str(self.venv / "python")),
                  mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty"), "HOME": str(home),
                                               "SHELL": str(self.tmp / "no-shell")})):
            p.start(); self.addCleanup(p.stop)
        paths.login_path.cache_clear()
        self.addCleanup(paths.login_path.cache_clear)

    def ctx(self, **cfg):
        base = {"metabot_bin": "", "orcha_bin": "", "metabot_python": ""}
        base.update(cfg)
        return mb.Ctx(base, self.tmp / "state")

    def test_empty_config_finds_the_binaries_beside_the_interpreter(self):
        c = self.ctx()
        self.assertEqual(c.metabot, str(self.beside))
        self.assertEqual(c.orcha, str(self.orcha))
        self.assertEqual(c.metabot_python, sys.executable)

    def test_empty_config_with_nothing_found_is_the_bare_name(self):
        """The command still says what it tried: "metabot", and the OSError names it."""
        self.beside.unlink()
        self.assertEqual(self.ctx().metabot, "metabot")

    def test_a_tilde_in_the_config_is_expanded(self):
        c = self.ctx(metabot_bin="~/tools/metabot", metabot_python="~/tools/python")
        self.assertEqual(c.metabot, str(self.home_bin))
        self.assertEqual(c.metabot_python, str(Path(os.environ["HOME"]) / "tools" / "python"))

    def test_an_absolute_config_path_is_kept_as_is(self):
        c = self.ctx(metabot_bin="/opt/x/metabot", orcha_bin="/opt/x/orcha", metabot_python="/opt/x/py")
        self.assertEqual((c.metabot, c.orcha, c.metabot_python), ("/opt/x/metabot", "/opt/x/orcha", "/opt/x/py"))


class LoginPathTests(unittest.TestCase):
    """A3: on macOS an app opened from Finder gets a bare PATH, so the PATH the user's
    login shell would have is read once and used for finding binaries."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="paths-login-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        paths.login_path.cache_clear()
        self.addCleanup(paths.login_path.cache_clear)

    def fake_shell(self, body: str) -> str:
        sh = self.tmp / "fakesh"
        sh.write_text("#!/bin/sh\n" + body + "\n")
        sh.chmod(0o755)
        return str(sh)

    def test_the_login_shell_path_is_used(self):
        sh = self.fake_shell('[ "$1" = "-lc" ] || exit 9; printf %s /from/login/shell:/usr/bin')
        with mock.patch.dict(os.environ, {"SHELL": sh, "PATH": "/plain"}):
            self.assertEqual(paths.login_path(), "/from/login/shell:/usr/bin")

    def test_a_slow_shell_falls_back_to_the_process_path(self):
        sh = self.fake_shell("sleep 1; printf %s /late")
        with mock.patch.dict(os.environ, {"SHELL": sh, "PATH": "/plain:/bin"}):
            self.assertEqual(paths.login_path(timeout=0.3), "/plain:/bin")

    def test_a_failing_or_missing_shell_falls_back(self):
        sh = self.fake_shell("exit 3")
        with mock.patch.dict(os.environ, {"SHELL": sh, "PATH": "/plain"}):
            self.assertEqual(paths.login_path(), "/plain")
        with mock.patch.dict(os.environ, {"SHELL": str(self.tmp / "nope"), "PATH": "/plain"}):
            paths.login_path.cache_clear()
            self.assertEqual(paths.login_path(), "/plain")

    def test_an_empty_answer_falls_back(self):
        sh = self.fake_shell("printf %s ''")
        with mock.patch.dict(os.environ, {"SHELL": sh, "PATH": "/plain"}):
            self.assertEqual(paths.login_path(), "/plain")

    def test_find_bin_searches_the_login_path(self):
        onlogin = self.tmp / "onlogin"
        tool = _exe(onlogin / "metabot")
        sh = self.fake_shell("printf %s " + str(onlogin))
        with mock.patch.dict(os.environ, {"SHELL": sh, "PATH": str(self.tmp / "empty")}), \
                mock.patch.object(sys, "executable", str(self.tmp / "nowhere" / "python")):
            self.assertEqual(paths.find_bin("metabot"), str(tool))


if __name__ == "__main__":
    unittest.main()
