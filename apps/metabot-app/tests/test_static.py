"""Static files: /vendor/ hardening (NUL bytes) and the read-only /tests/ route
that lets the tablet run the parity self-test."""
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, ROOT  # noqa: E402


class StaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def raw_get(self, path):
        """Headers and body of one raw request (the path may carry bytes a
        client library would refuse)."""
        import re
        s = socket.create_connection(("127.0.0.1", self.env.port), timeout=10)
        s.sendall(f"GET {path}?t={self.env.token} HTTP/1.1\r\nHost: {self.env.host}\r\n\r\n".encode())
        data = b""
        s.settimeout(5)
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            m = re.search(rb"Content-Length: (\d+)", data)
            want = int(m.group(1)) if m else 0
            head, body = data.split(b"\r\n\r\n", 1)
            while len(body) < want:
                chunk = s.recv(4096)
                if not chunk:
                    break
                body += chunk
            data = head + b"\r\n\r\n" + body
        except OSError:
            pass
        s.close()
        return data

    def test_nul_byte_in_path_is_400_not_a_dropped_connection(self):
        for path in ("/vendor/\x00x", "/tests/\x00x", "/api/mb/run/\x00"):
            data = self.raw_get(path)
            self.assertTrue(data.startswith(b"HTTP/1.1 400"), (path, data[:80]))
            self.assertIn(b'"error": "bad path"', data)

    def test_selftest_page_and_cases_served_under_tests(self):
        s, j, raw, headers = self.env.request("GET", "/tests/verify-selftest.html")
        self.assertEqual(s, 200)
        self.assertEqual(raw, (ROOT / "selftest" / "verify-selftest.html").read_bytes())
        h = dict((k.lower(), v) for k, v in headers)
        self.assertEqual(h["content-type"], "text/html; charset=utf-8")
        self.assertEqual(h["cache-control"], "no-store")
        s, j, raw, headers = self.env.request("GET", "/tests/fixtures/parity/cases.json")
        self.assertEqual(s, 200)
        self.assertEqual(raw, (ROOT / "selftest" / "fixtures" / "parity" / "cases.json").read_bytes())
        self.assertEqual(dict((k.lower(), v) for k, v in headers)["content-type"], "application/json")
        # the page's relative paths resolve under /tests/ and /vendor/
        page = (ROOT / "selftest" / "verify-selftest.html").read_text()
        self.assertIn("'./fixtures/parity/cases.json'", page)
        self.assertIn("'../vendor/mbverify.js'", page)
        s, j = self.env.get("/vendor/mbverify.js")
        self.assertEqual(s, 200)

    def test_tests_route_serves_only_the_two_files(self):
        for path in ("/tests/test_auth.py", "/tests/fixtures/golden.json", "/tests/fixtures/harness.py",
                     "/tests/../server.py", "/tests/", "/tests/verify-selftest.html/..",
                     "/tests/fixtures/parity/../../verify-selftest.html", "/tests/nope.html"):
            s, j = self.env.get(path)
            self.assertEqual((s, j), (404, {"ok": False, "error": "not found"}), path)

    def test_tests_route_is_token_gated(self):
        s, j = self.env.get("/tests/verify-selftest.html", token=False)
        self.assertEqual((s, j), (403, {"ok": False, "error": "bad token"}))


if __name__ == "__main__":
    unittest.main()
