"""C4 routes: the desktop starts a code on loopback, the phone claims it on the TLS
listener without a token, and gets a device token of its own. Pairing and device
management never answer on the phone scope; claiming never answers on loopback."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import pairing  # noqa: E402

PAYLOAD_RX = re.compile(r"^metabot-pair:1\?a=([^&]+)&p=(\d+)&c=([0-9A-HJKMNP-TV-Z]{8})&f=([0-9a-f]{64})$")


class PairRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(phone=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        server.Handler._fail.clear()

    tearDown = setUp

    def claim(self, code, name="Test phone", **kw):
        # as the APK does it: JSON body, no token, no X-MetaChat header
        return self.env.tls_request("POST", "/api/pair", {"code": code, "name": name},
                                    headers={"X-MetaChat": None}, **kw)

    def test_start_then_claim_then_use(self):
        s, j = self.env.post("/api/pair/start", {})
        self.assertEqual(s, 200, j)
        self.assertEqual(sorted(j), ["addresses", "code", "expires_in", "fingerprint", "ok", "payload", "port", "words"])
        self.assertEqual(j["port"], self.env.tls_port)
        self.assertEqual(j["fingerprint"], self.env.fingerprint)
        self.assertEqual(j["words"], pairing.words(self.env.fingerprint))
        m = PAYLOAD_RX.match(j["payload"])
        self.assertIsNotNone(m, j["payload"])
        self.assertEqual((int(m.group(2)), m.group(3), m.group(4)), (j["port"], j["code"], j["fingerprint"]))
        self.assertTrue(self.env.get("/api/meta")[1]["phone"]["pairing"])

        s, r, _, _ = self.claim(j["code"].lower(), "SM-TEST")
        self.assertEqual(s, 200, r)
        self.assertEqual((r["ok"], r["name"]), (True, "SM-TEST"))
        token = r["token"]
        s, meta, _, _ = self.env.tls_request("GET", "/api/meta", token=token)
        self.assertEqual((s, meta["scope"], meta["device"]["name"]), (200, "phone", "SM-TEST"))
        s, meta = self.env.get("/api/meta")
        self.assertEqual((meta["phone"]["on"], meta["phone"]["pairing"], meta["phone"]["fingerprint"]),
                         (True, False, self.env.fingerprint))
        self.assertGreaterEqual(meta["phone"]["devices"], 1)
        self.assertIsNone(meta["device"])
        s, d = self.env.get("/api/devices")
        self.assertIn("SM-TEST", [x["name"] for x in d["devices"]])
        self.assertFalse(any("hash" in x for x in d["devices"]))
        # consumed
        s, r, _, _ = self.claim(j["code"])
        self.assertEqual((s, r), (403, {"ok": False, "error": "bad code"}))
        server.Handler._fail.clear()
        # revoke: the token stops working
        dev_id = next(x["id"] for x in d["devices"] if x["name"] == "SM-TEST")
        self.assertEqual(self.env.post("/api/devices/revoke", {"id": dev_id}), (200, {"ok": True}))
        self.assertEqual(self.env.post("/api/devices/revoke", {"id": dev_id})[0], 404)
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=token)
        self.assertEqual(s, 403)

    def test_scopes_of_the_routes(self):
        token, _ = self.env.pair_device()
        s, j, _, _ = self.env.tls_request("POST", "/api/pair/start", {}, token=token)
        self.assertEqual((s, j), (403, {"ok": False, "error": "desktop only"}))
        s, j, _, _ = self.env.tls_request("GET", "/api/devices", token=token)
        self.assertEqual((s, j), (403, {"ok": False, "error": "desktop only"}))
        s, j, _, _ = self.env.tls_request("POST", "/api/devices/revoke", {"id": "x"}, token=token)
        self.assertEqual((s, j), (403, {"ok": False, "error": "desktop only"}))
        s, j = self.env.post("/api/pair", {"code": "ABCDEFGH", "name": "x"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "phone only"}))

    def test_wrong_claims_back_off(self):
        self.env.post("/api/pair/start", {})
        s, r, _, _ = self.claim("ABCDEFGH")
        self.assertEqual((s, r), (403, {"ok": False, "error": "bad code"}))
        s, r, _, _ = self.claim("ABCDEFGH")            # locked out for a few seconds
        self.assertEqual((s, r), (429, {"ok": False, "error": "slow down"}))
        self.assertIn(("phone", "127.0.0.1", "code"), server.Handler._fail)

    def test_claim_needs_a_json_object(self):
        self.env.post("/api/pair/start", {})
        s, r, _, _ = self.env.tls_request("POST", "/api/pair", raw=b"[]", headers={"X-MetaChat": None})
        self.assertEqual(s, 400)
        s, r, _, _ = self.env.tls_request("POST", "/api/pair", raw=b"{", headers={"X-MetaChat": None})
        self.assertEqual(s, 400)


class ListenerOffTests(unittest.TestCase):
    def test_start_says_off(self):
        env = Env(config={"phone_listener": False}).start()
        try:
            self.assertEqual(env.post("/api/pair/start", {}), (503, {"ok": False, "error": "phone listener off"}))
            meta = env.get("/api/meta")[1]
            self.assertEqual((meta["phone"]["on"], meta["phone"]["fingerprint"]), (False, None))
        finally:
            env.stop()


if __name__ == "__main__":
    unittest.main()
