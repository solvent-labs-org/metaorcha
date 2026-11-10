"""C4: pairing codes. 8 Crockford base32 characters, 10 minutes, one live code at a
time, dead after 5 wrong tries or one right one. The payload is the string the QR
carries and the APK parses; the four words are the APK's, from the fingerprint."""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import devices, pairing  # noqa: E402

FP = "ab" * 32
PAYLOAD_RX = re.compile(r"^metabot-pair:1\?a=([^&]+)&p=(\d+)&c=([0-9A-HJKMNP-TV-Z]{8})&f=([0-9a-f]{64})$")


class NormalizeTests(unittest.TestCase):
    def test_like_the_apk(self):
        self.assertEqual(pairing.normalize(" abcd-efgh "), "ABCDEFGH")
        self.assertEqual(pairing.normalize("io1l o0lI"), "1011O0L1".replace("O", "0").replace("L", "1"))
        self.assertIsNone(pairing.normalize("ABCDEFG"))        # 7
        self.assertIsNone(pairing.normalize("ABCDEFGHJ"))      # 9
        self.assertIsNone(pairing.normalize("ABCDEFGU"))       # U is not Crockford
        self.assertIsNone(pairing.normalize(None))
        self.assertIsNone(pairing.normalize(12345678))

    def test_payload_and_words(self):
        text = pairing.payload(["192.0.2.10", "198.51.100.9"], 8461, "ABCDEFGH", FP)
        m = PAYLOAD_RX.match(text)
        self.assertIsNotNone(m, text)
        self.assertEqual(m.group(1), "192.0.2.10%2C198.51.100.9")
        self.assertEqual((m.group(2), m.group(3), m.group(4)), ("8461", "ABCDEFGH", FP))
        self.assertEqual(len(pairing.WORDS), 256)
        self.assertEqual(pairing.words(FP), [pairing.WORDS[0xAB]] * 4)
        self.assertEqual(pairing.words("00" + "01" + "ff" + "7f" + "00" * 28),
                         [pairing.WORDS[0], pairing.WORDS[1], pairing.WORDS[255], pairing.WORDS[127]])
        self.assertEqual(pairing.WORDS[:4], ["apple", "river", "stone", "cloud"])
        self.assertEqual(pairing.WORDS[-1], "glass")


class CodeTests(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        root = Path(tempfile.mkdtemp(prefix="metachat-pair-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.dev = devices.Devices(root / "devices.json")
        self.pair = pairing.Pairing(self.dev)

    def test_start_and_claim(self):
        live = self.pair.start()
        self.assertRegex(live["code"], r"^[0-9A-HJKMNP-TV-Z]{8}$")
        self.assertEqual(live["expires_in"], pairing.TTL)
        status, reply = self.pair.claim(live["code"].lower(), "Test phone")
        self.assertEqual(status, 200)
        self.assertEqual(reply["ok"], True)
        self.assertEqual(reply["name"], "Test phone")
        self.assertEqual(self.dev.match(reply["token"], "phone")["id"], reply["id"])
        status, reply = self.pair.claim(live["code"], "Again")       # consumed
        self.assertEqual((status, reply), (403, {"ok": False, "error": "bad code"}))
        self.assertEqual(self.dev.count("phone"), 1)

    def test_a_new_start_replaces_the_old_code(self):
        first = self.pair.start()["code"]
        second = self.pair.start()["code"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.pair.claim(first, "x")[0], 403)
        self.assertEqual(self.pair.claim(second, "x")[0], 200)

    def test_five_wrong_tries_kill_the_code(self):
        code = self.pair.start()["code"]
        wrong = ("A" if code[0] != "A" else "B") + code[1:]
        for _ in range(pairing.TRIES - 1):
            self.assertEqual(self.pair.claim(wrong, "x")[0], 403)
        self.assertTrue(self.pair.status()["live"])
        self.assertEqual(self.pair.claim(wrong, "x")[0], 403)      # the 5th
        self.assertFalse(self.pair.status()["live"])
        self.assertEqual(self.pair.claim(code, "x")[0], 403)       # right, but dead
        self.assertEqual(self.dev.count("phone"), 0)

    def test_malformed_and_expired(self):
        code = self.pair.start()["code"]
        self.assertEqual(self.pair.claim("short", "x")[0], 403)
        self.assertEqual(self.pair.claim(None, "x")[0], 403)
        self.assertTrue(self.pair.status()["live"])                 # malformed is not a try
        with mock.patch.object(pairing.time, "time", return_value=pairing.time.time() + pairing.TTL + 1):
            self.assertFalse(self.pair.status()["live"])
            self.assertEqual(self.pair.claim(code, "x")[0], 403)
        self.assertEqual(self.pair.claim("ABCDEFGH", "x"), (403, {"ok": False, "error": "bad code"}))

    def test_nothing_live_is_a_refusal(self):
        self.assertEqual(self.pair.claim("ABCDEFGH", "x"), (403, {"ok": False, "error": "bad code"}))
        self.assertEqual(self.pair.status(), {"live": False, "expires_in": 0})


if __name__ == "__main__":
    unittest.main()
