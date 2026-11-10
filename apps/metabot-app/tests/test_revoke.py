"""C5: removing a device closes its live connections. A phone with an open event
stream or terminal socket is cut within a second of the revoke, and its next request
is refused; other devices' sockets stay up."""
import sys
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


def _eof_within(s, seconds):
    s.settimeout(seconds)
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return True
    except TimeoutError:
        return False
    except OSError:
        return True


class RevokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(phone=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        server.Handler._fail.clear()

    def stream(self, token):
        s = self.env.tls_socket()
        s.sendall((f"GET /api/stream?t={token} HTTP/1.1\r\nHost: {self.env.tls_host}\r\n\r\n").encode())
        self.assertIn(b"event: hello", _read_until(s, b"event: hello"))
        return s

    def test_revoke_cuts_the_stream_and_the_terminal_of_that_device_only(self):
        gone_tok, gone = self.env.pair_device("Going")
        stay_tok, stay = self.env.pair_device("Staying")
        s_gone = self.stream(gone_tok)
        s_stay = self.stream(stay_tok)
        self.env.add_session("fc-ws-rev", pid=4100001, cwd="/")
        ws, status, rest = self.env.tls_ws("/ws/term?desk=ws-rev", gone_tok)
        self.assertEqual(status, 101)
        self.assertEqual(server.live_connections(gone["id"]), 2)
        self.assertEqual(self.env.post("/api/devices/revoke", {"id": gone["id"]}), (200, {"ok": True}))
        self.assertTrue(_eof_within(s_gone, 3))
        try:
            code, _ = ws_read_close(ws, rest, timeout=3)
            self.assertEqual(code, 4003)          # "device removed"
        except (ConnectionError, OSError):
            pass                                  # cut without a frame is also acceptable
        self.assertEqual(server.live_connections(gone["id"]), 0)
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=gone_tok)
        self.assertEqual(s, 403)
        server.Handler._fail.clear()      # that refusal counted as a bad token from this address
        # the other device is untouched
        s_stay.settimeout(0.5)
        with self.assertRaises(TimeoutError):
            s_stay.recv(10)
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=stay_tok)
        self.assertEqual(s, 200)
        s_stay.close()
        s_gone.close()
        ws.close()
        end = time.time() + 3
        while server.live_connections(stay["id"]) and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(server.live_connections(stay["id"]), 0)   # closed sockets leave the table

    def test_a_revoked_phone_never_locks_out_the_desktop_on_the_same_address(self):
        # Over `adb reverse` (and on any shared host) the phone and the desktop CLI share an
        # address; the phone's stale-token 403s must not block the desktop's next call.
        tok, dev = self.env.pair_device("Stale")
        self.assertEqual(self.env.post("/api/devices/revoke", {"id": dev["id"]}), (200, {"ok": True}))
        for _ in range(3):
            s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=tok)
            self.assertEqual(s, 403)
        self.assertEqual(self.env.get("/api/meta")[0], 200)
        self.assertEqual(self.env.post("/api/pair/start", {})[0], 200)


    def test_a_stale_token_never_blocks_a_new_pairing_from_the_same_phone(self):
        # The phone that was revoked retries with its old token a few times before it
        # reaches the pairing screen; those are not pairing-code guesses and must not
        # earn it a "slow down" when it pairs again seconds later.
        tok, dev = self.env.pair_device("Stale")
        self.assertEqual(self.env.post("/api/devices/revoke", {"id": dev["id"]}), (200, {"ok": True}))
        for _ in range(4):
            s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=tok)
            self.assertEqual(s, 403)
        s, j = self.env.post("/api/pair/start", {})
        self.assertEqual(s, 200)
        s, r, _, _ = self.env.tls_request("POST", "/api/pair", {"code": j["code"], "name": "Again"},
                                          headers={"X-MetaChat": None})   # as the APK claims
        self.assertEqual(s, 200, r)
        self.assertNotEqual(r["id"], dev["id"])
        s, _, _, _ = self.env.tls_request("GET", "/api/meta", token=r["token"])
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
