"""C2: the phone listener. TLS with the host's pinned certificate, phone-scope device
tokens only, no Host check (the pin and the token are the gate), Secure cookies, and
SSE/WS that work over TLS. A plain-HTTP client on this port is dropped quietly."""
import contextlib
import hashlib
import io
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server, ws_read_close  # noqa: E402


def _read_until(s, marker, timeout=5):
    s.settimeout(timeout)
    buf = b""
    end = time.time() + timeout
    while marker not in buf and time.time() < end:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


class TLSListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(phone=True).start()
        cls.token, cls.row = cls.env.pair_device("Test phone")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        server.Handler._fail.clear()

    tearDown = setUp

    def test_the_listener_presents_the_pinned_certificate(self):
        with self.env.tls_socket() as s:
            seen = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
        self.assertEqual(seen, self.env.fingerprint)
        self.assertEqual(server.phone_cert().fingerprint, self.env.fingerprint)

    def test_scopes_do_not_cross(self):
        s, j, _, _ = self.env.tls_request("GET", "/api/meta", token=self.env.token)   # desktop token
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))
        server.Handler._fail.clear()
        s, j = self.env.get(f"/api/meta?t={self.token}", token=False)                # phone token on loopback
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))
        server.Handler._fail.clear()
        s, j, _, _ = self.env.tls_request("GET", "/api/meta", token=self.token)
        self.assertEqual(s, 200)
        self.assertEqual(j["scope"], "phone")
        s, j = self.env.get("/api/meta")
        self.assertEqual((s, j["scope"]), (200, "desktop"))

    def test_no_token_is_403_on_tls_too(self):
        s, j, _, _ = self.env.tls_request("GET", "/api/meta")
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))

    def test_any_host_header_on_the_phone_listener(self):
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=self.token, host="192.0.2.9:1")
        self.assertEqual(s, 200)
        s, j = self.env.get("/api/meta", host="192.0.2.9:1")       # loopback keeps its Host gate
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad host"}))

    def test_the_cookie_is_secure_on_tls_and_works(self):
        _, _, _, headers = self.env.tls_request("GET", "/api/meta", token=self.token)
        cookie = [v for k, v in headers if k.lower() == "set-cookie"][0]
        self.assertTrue(cookie.startswith(f"fc_t={self.token};"))
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        _, _, _, headers = self.env.request("GET", "/api/meta")
        self.assertNotIn("Secure", [v for k, v in headers if k.lower() == "set-cookie"][0])
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", headers={"Cookie": f"fc_t={self.token}"})
        self.assertEqual(s, 200)

    def test_post_over_tls_keeps_the_csrf_rules(self):
        s, j, _, _ = self.env.tls_request("POST", "/api/kill", {"name": "x", "confirm": True}, token=self.token)
        self.assertEqual(s, 404)        # past CSRF: unknown session
        s, j, _, _ = self.env.tls_request("POST", "/api/kill", {"name": "x"}, token=self.token,
                                          headers={"X-MetaChat": None})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))

    def test_plain_http_on_the_tls_port_is_dropped_quietly(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            raw = socket.create_connection(("127.0.0.1", self.env.tls_port), timeout=5)
            raw.sendall(b"GET /api/meta HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                got = raw.recv(100)
            except OSError:
                got = b""
            raw.close()
            time.sleep(0.2)
        self.assertEqual(got, b"")
        self.assertNotIn("Traceback", err.getvalue())
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=self.token)   # still serving
        self.assertEqual(s, 200)

    def test_websocket_over_tls(self):
        s, status, rest = self.env.tls_ws("/ws/term?desk=ghost", self.token)
        self.assertEqual(status, 101)
        code, _ = ws_read_close(s, rest)
        self.assertEqual(code, 4004)
        s.close()

    def test_sse_over_tls_and_a_gone_phone_is_noticed(self):
        before = threading.active_count()
        s = self.env.tls_socket()
        s.sendall((f"GET /api/stream?t={self.token} HTTP/1.1\r\nHost: {self.env.tls_host}\r\n\r\n").encode())
        head = _read_until(s, b"event: hello")
        self.assertIn(b"event: hello", head)
        s.close()
        end = time.time() + 5
        while threading.active_count() > before and time.time() < end:
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before)   # the handler thread ended within 5 s


if __name__ == "__main__":
    unittest.main()
