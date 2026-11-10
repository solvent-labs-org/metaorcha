"""B8: the server reports what the host can do; the page branches on these and never
on the operating system. Each capability is a fact read from the host (a binary found,
a listener up, an interpreter inside a phone app), cached briefly."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import paths, tmuxcmd  # noqa: E402

KEYS = ["camera", "desks", "display", "engines", "on_device", "phone_listener", "sandbox", "service"]


class CapsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(phone=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        server.invalidate_caps()

    def test_meta_carries_the_capability_block(self):
        s, j = self.env.get("/api/meta")
        caps = j["caps"]
        self.assertEqual(sorted(caps), KEYS)
        self.assertTrue(caps["desks"])                      # the fake tmux (3.4) is on PATH
        self.assertEqual(caps["engines"]["openai"], "http")
        self.assertIn(caps["service"], ("systemd", "launchd", "android", "none"))
        self.assertIs(caps["camera"], False)
        self.assertIs(caps["on_device"], False)
        self.assertIs(caps["phone_listener"], True)
        self.assertIsInstance(caps["sandbox"], bool)
        self.assertIsInstance(caps["display"], bool)

    def test_no_tmux_means_no_desks(self):
        with mock.patch.object(tmuxcmd, "found", return_value=None):
            server.invalidate_caps()
            self.assertFalse(self.env.get("/api/meta")[1]["caps"]["desks"])
        with mock.patch.object(tmuxcmd, "version", return_value=(3, 1)):
            server.invalidate_caps()
            self.assertFalse(self.env.get("/api/meta")[1]["caps"]["desks"])   # too old to drive

    def test_engines_follow_the_binaries(self):
        real = paths.find_bin

        def fake(name):
            if name in ("claude", "cursor-agent"):
                return f"/fake/{name}"
            if name == "codex":
                return None
            return real(name)

        with mock.patch.object(paths, "find_bin", side_effect=fake):
            server.invalidate_caps()
            eng = self.env.get("/api/meta")[1]["caps"]["engines"]
        self.assertEqual((eng["claude"], eng["cursor"], eng["openai"]), ("cli", "cli", "http"))
        self.assertNotIn("codex", eng)

    def test_on_device_is_the_phone_runtime(self):
        with mock.patch.dict(os.environ, {"METABOT_ON_DEVICE": "1"}):
            server.invalidate_caps()
            caps = self.env.get("/api/meta")[1]["caps"]
        self.assertIs(caps["on_device"], True)
        self.assertIs(caps["phone_listener"], False)        # pairing is hidden on the phone itself
        self.assertEqual(caps["service"], "android")

    def test_phone_listener_off_in_config(self):
        env = Env(config={"phone_listener": False}).start()
        try:
            self.assertIs(env.get("/api/meta")[1]["caps"]["phone_listener"], False)
        finally:
            env.stop()

    def test_caps_are_cached_briefly(self):
        self.env.get("/api/meta")
        with mock.patch.object(tmuxcmd, "found", return_value=None):
            self.assertTrue(self.env.get("/api/meta")[1]["caps"]["desks"])    # still the cached answer
            server.invalidate_caps()
            self.assertFalse(self.env.get("/api/meta")[1]["caps"]["desks"])


if __name__ == "__main__":
    unittest.main()
