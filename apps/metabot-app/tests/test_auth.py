"""Section 3: host, token, CSRF, body limits, error shape, cookie flags."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env  # noqa: E402


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_wrong_token_403(self):
        s, j = self.env.get("/api/meta?t=nope", token=False)
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))

    def test_wrong_query_token_never_sets_the_cookie(self):
        # SEC-1: the 403 for a guessed ?t= must not pin fc_t=<real token>
        from harness import server
        server.Handler._fail.clear()
        s, j, _, headers = self.env.request("GET", "/?t=guess", token=False)
        server.Handler._fail.clear()   # undo the lockout for the tests that follow
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))
        self.assertEqual([v for k, v in headers if k.lower() == "set-cookie"], [])
        # a good query token still pins it; a good cookie does not re-send it
        _, _, _, headers = self.env.request("GET", "/api/meta")
        self.assertEqual(len([v for k, v in headers if k.lower() == "set-cookie"]), 1)
        _, _, _, headers = self.env.request("GET", "/api/meta", token=False,
                                            headers={"Cookie": f"fc_t={self.env.token}"})
        self.assertEqual([v for k, v in headers if k.lower() == "set-cookie"], [])

    def test_missing_token_403_no_lockout(self):
        s, j = self.env.get("/api/meta", token=False)
        self.assertEqual(s, 403)
        s, j = self.env.get("/api/meta")
        self.assertEqual(s, 200)

    def test_bad_host_403_before_token(self):
        s, j = self.env.get("/api/meta?t=nope", token=False, host="evil.example:1")
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad host"}))
        s, j = self.env.post("/api/kill", {"name": "x", "confirm": True}, host="evil.example:1")
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad host"}))

    def test_localhost_host_allowed(self):
        s, _ = self.env.get("/api/meta", host=f"localhost:{self.env.port}")
        self.assertEqual(s, 200)

    def test_cookie_is_httponly_strict(self):
        _, _, _, headers = self.env.request("GET", "/api/meta")
        cookie = [v for k, v in headers if k.lower() == "set-cookie"][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertTrue(cookie.startswith("fc_t="))

    def test_cookie_auth_works(self):
        s, _ = self.env.get("/api/meta", token=False, headers={"Cookie": f"fc_t={self.env.token}"})
        self.assertEqual(s, 200)

    def test_post_without_header_403(self):
        s, j = self.env.post("/api/kill", {"name": "x", "confirm": True}, headers={"X-MetaChat": None})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))

    def test_post_wrong_content_type_403(self):
        s, j = self.env.post("/api/kill", {"name": "x"}, headers={"Content-Type": "text/plain"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))

    def test_post_charset_suffix_ok(self):
        s, j = self.env.post("/api/kill", {"name": "x", "confirm": True},
                             headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(s, 404)   # past CSRF: unknown session

    def test_foreign_origin_403(self):
        s, j = self.env.post("/api/kill", {"name": "x"}, headers={"Origin": "http://evil.example:1"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))
        s, j = self.env.post("/api/kill", {"name": "x"}, headers={"Origin": "null"})
        self.assertEqual(s, 403)

    def test_same_origin_ok(self):
        s, j = self.env.post("/api/kill", {"name": "x", "confirm": True},
                             headers={"Origin": f"http://{self.env.host}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(s, 404)

    def test_sec_fetch_site_cross_403(self):
        s, j = self.env.post("/api/kill", {"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))

    def test_body_too_large_413(self):
        raw = json.dumps({"pad": "x" * 70000}).encode()
        s, j = self.env.post("/api/kill", raw=raw)
        self.assertEqual((s, j), (413, {"ok": False, "error": "too large"}))

    def test_negative_length_400(self):
        s, j = self.env.post("/api/kill", raw=b"", headers={"Content-Length": "-1"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad length"}))

    def test_missing_length_400(self):
        s, j = self.env.post("/api/kill", raw=None, headers={"Content-Length": None})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad length"}))

    def test_bad_json_400(self):
        s, j = self.env.post("/api/kill", raw=b"[1,2]")
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad json"}))
        s, j = self.env.post("/api/kill", raw=b"{nope")
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad json"}))

    def test_unknown_routes_404_shape(self):
        s, j = self.env.get("/api/nope")
        self.assertEqual((s, j), (404, {"ok": False, "error": "not found"}))
        s, j = self.env.post("/api/nope", {})
        self.assertEqual((s, j), (404, {"ok": False, "error": "not found"}))

    def test_vendor_serves_mjs_and_refuses_traversal(self):
        s, j = self.env.get("/vendor/../server.py")
        self.assertEqual(s, 404)
        s, j = self.env.get("/vendor/xterm.css")
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
