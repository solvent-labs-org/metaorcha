"""C3: one token per paired device. Tokens are stored as sha256 hashes in
STATE/devices.json (0600); a row has an id, a name, a scope and times. match() is
constant-time per row and scope-bound: a phone token never opens the desktop scope."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import devices  # noqa: E402


class DevicesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="metachat-dev-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "devices.json"
        self.dev = devices.Devices(self.path)

    def test_add_returns_a_token_and_a_row(self):
        token, row = self.dev.add("Test phone", "phone")
        self.assertGreaterEqual(len(token), 24)
        self.assertNotIn(token, self.path.read_text())          # only the hash is on disk
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertRegex(row["id"], r"^[0-9a-f]{8}$")
        self.assertEqual((row["name"], row["scope"]), ("Test phone", "phone"))
        self.assertIsInstance(row["created"], (int, float))
        self.assertNotIn("hash", row)

    def test_match_is_scope_bound(self):
        token, row = self.dev.add("Test phone", "phone")
        self.assertEqual(self.dev.match(token, "phone")["id"], row["id"])
        self.assertIsNone(self.dev.match(token, "desktop"))
        self.assertIsNone(self.dev.match(token[:-1] + ("a" if token[-1] != "a" else "b"), "phone"))
        self.assertIsNone(self.dev.match("", "phone"))
        self.assertIsNone(self.dev.match(None, "phone"))

    def test_rows_persist_and_list_hides_hashes(self):
        token, row = self.dev.add("Test phone", "phone")
        again = devices.Devices(self.path)
        self.assertEqual(again.match(token, "phone")["id"], row["id"])
        listed = again.list()
        self.assertEqual([r["id"] for r in listed], [row["id"]])
        self.assertFalse(any("hash" in r for r in listed))
        self.assertEqual(sorted(listed[0]), ["created", "id", "name", "scope", "seen"])

    def test_revoke(self):
        token, row = self.dev.add("Test phone", "phone")
        self.assertTrue(self.dev.revoke(row["id"]))
        self.assertIsNone(self.dev.match(token, "phone"))
        self.assertEqual(self.dev.list(), [])
        self.assertFalse(self.dev.revoke(row["id"]))
        self.assertFalse(self.dev.revoke("nope"))

    def test_names_are_trimmed_and_bounded(self):
        _, row = self.dev.add("   ", "phone")
        self.assertEqual(row["name"], "device")
        _, row = self.dev.add(" x" * 60, "phone")
        self.assertLessEqual(len(row["name"]), 40)
        _, row = self.dev.add("line\nbreak\ttab", "phone")
        self.assertEqual(row["name"], "line break tab")
        with self.assertRaises(ValueError):
            self.dev.add("p", "root")

    def test_seen_is_touched_on_match(self):
        token, row = self.dev.add("Test phone", "phone")
        self.assertIsNone(row["seen"])
        self.dev.match(token, "phone")
        self.assertIsInstance(devices.Devices(self.path).list()[0]["seen"], (int, float))

    def test_a_corrupt_file_starts_empty_and_is_kept_aside(self):
        self.path.write_text("{not json")
        dev = devices.Devices(self.path)
        self.assertEqual(dev.list(), [])
        dev.add("Test phone", "phone")
        self.assertEqual(len(dev.list()), 1)
        self.assertEqual(json.loads(self.path.read_text())["v"], 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["devices.json", "devices.json.bad"])

    def test_write_is_atomic(self):
        self.dev.add("Test phone", "phone")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["devices.json"])
        os.chmod(self.root, 0o500)      # the folder refuses the temp file: the old file survives
        try:
            with self.assertRaises(OSError):
                self.dev.add("Second", "phone")
        finally:
            os.chmod(self.root, 0o700)
        self.assertEqual(len(devices.Devices(self.path).list()), 1)


if __name__ == "__main__":
    unittest.main()
