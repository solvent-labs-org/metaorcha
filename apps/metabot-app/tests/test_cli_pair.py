"""A7 (part): ``metabot-app pair`` and ``metabot-app devices`` talk to the running server
on loopback with the desktop token, so a phone can be paired from a terminal when the
page is not open. They never start a server of their own."""
import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import cli, pairing  # noqa: E402


class CliPairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(phone=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_pair_prints_what_the_phone_needs(self):
        rc, out, err = self.run_cli("pair", "--port", str(self.env.port))
        self.assertEqual(rc, 0, err)
        m = re.search(r"Code:\s+([0-9A-HJKMNP-TV-Z]{4}) ([0-9A-HJKMNP-TV-Z]{4})", out)
        self.assertIsNotNone(m, out)
        self.assertIn(" ".join(pairing.words(self.env.fingerprint)), out)
        self.assertIn(f":{self.env.tls_port}", out)
        self.assertIn("metabot-pair:1?", out)
        self.assertNotIn(self.env.token, out)
        self.assertTrue(server.PAIR.status()["live"])

    def test_devices_lists_and_removes(self):
        token, row = self.env.pair_device("CLI phone")
        rc, out, _ = self.run_cli("devices", "--port", str(self.env.port))
        self.assertEqual(rc, 0)
        self.assertIn("CLI phone", out)
        self.assertIn(row["id"], out)
        self.assertNotIn(token, out)
        rc, out, _ = self.run_cli("devices", "--port", str(self.env.port), "--remove", row["id"])
        self.assertEqual(rc, 0)
        self.assertIsNone(server.DEVICES.match(token, "phone"))
        rc, _, err = self.run_cli("devices", "--port", str(self.env.port), "--remove", row["id"])
        self.assertEqual(rc, 1)
        self.assertIn("no such device", err)

    def test_no_server_is_a_plain_message(self):
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        rc, out, err = self.run_cli("pair", "--port", str(port))
        self.assertEqual(rc, 1)
        self.assertIn("not running", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
