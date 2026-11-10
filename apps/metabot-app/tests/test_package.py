"""A1: the app builds as one wheel that carries its page, vendored code, modules and
self-test corpus, and installs a ``metabot-app`` command."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))
from metabot_app import cli  # noqa: E402

UV = shutil.which("uv")


class CliTests(unittest.TestCase):
    def test_version_prints_the_package_version(self):
        import contextlib, io
        from metabot_app import __version__
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_no_command_prints_usage_and_exits_2(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("usage", err.getvalue().lower())


@unittest.skipUnless(UV, "uv not on PATH")
class WheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        r = subprocess.run([UV, "build", "--wheel", "--out-dir", cls.tmp.name, str(APP)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise AssertionError("uv build failed:\n" + r.stderr[-2000:])
        cls.wheel = next(Path(cls.tmp.name).glob("*.whl"))
        cls.names = set(zipfile.ZipFile(cls.wheel).namelist())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_wheel_is_named_metaorcha_app(self):
        self.assertTrue(self.wheel.name.startswith("metaorcha_app-"), self.wheel.name)

    def test_wheel_carries_the_page_vendor_modules_and_selftest(self):
        for need in ("metabot_app/index.html", "metabot_app/server.py", "metabot_app/mbq.py",
                     "metabot_app/vendor/mbverify.js", "metabot_app/vendor/xterm.js",
                     "metabot_app/modules/runs.json",
                     "metabot_app/selftest/verify-selftest.html",
                     "metabot_app/selftest/fixtures/parity/cases.json"):
            self.assertIn(need, self.names)
        self.assertTrue(any(n.startswith("metabot_app/vendor/noble/") for n in self.names))

    def test_wheel_has_no_tests_or_caches(self):
        bad = [n for n in self.names if "__pycache__" in n or n.startswith("tests/") or n.endswith(".pyc")]
        self.assertEqual(bad, [])

    def test_wheel_declares_the_metabot_app_command(self):
        ep = next(n for n in self.names if n.endswith("entry_points.txt"))
        text = zipfile.ZipFile(self.wheel).read(ep).decode()
        self.assertIn("metabot-app = metabot_app.cli:main", text)


if __name__ == "__main__":
    unittest.main()
