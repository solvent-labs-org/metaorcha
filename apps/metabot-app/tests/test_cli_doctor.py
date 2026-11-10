"""A7 (part): ``metabot-app doctor [--json]`` reads the host without a server: the ten
items in plain words, exit 0 when all is well and 1 on a blocker."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import cli, paths, tmuxcmd  # noqa: E402


class CliDoctorTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.addCleanup(self.env.stop)
        key = self.env.root / "key"
        key.write_text("k\n")
        key.chmod(0o600)
        os.environ["ORCHA_KEY_PATH"] = str(key)
        self.addCleanup(os.environ.pop, "ORCHA_KEY_PATH", None)
        os.environ["PATH"] = f"{self.env.bin}:/usr/bin:/bin"
        for p in (mock.patch.object(paths, "login_path", return_value=""),
                  mock.patch.object(tmuxcmd, "version", return_value=(3, 4))):
            p.start()
            self.addCleanup(p.stop)
        claude = self.env.bin / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n")
        claude.chmod(0o755)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_text_report_exits_zero_when_well(self):
        rc, out, err = self.run_cli("doctor")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ok   core", out)
        self.assertIn("MetaBot 0.3.0", out)
        self.assertIn("key", out)
        self.assertNotIn(str(self.env.root / "key"), out)

    def test_json_report_and_a_blocker(self):
        rc, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(rc, 0)
        rep = json.loads(out)
        self.assertTrue(rep["ok"])
        self.assertEqual(len(rep["items"]), 10)
        (self.env.root / "key").unlink()
        rc, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(rc, 1)
        rep = json.loads(out)
        self.assertFalse(rep["ok"])
        self.assertIn("metabot init", [i for i in rep["items"] if i["id"] == "key"][0]["fix"])
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL key", out)
        self.assertIn("fix: run: metabot init", out)


if __name__ == "__main__":
    unittest.main()
