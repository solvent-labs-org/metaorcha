"""C1: the phone listener's certificate. One self-signed P-256 pair per host under
STATE/tls, made once, key 0600, pinned by the phone through sha256(DER) = the f= part
of a pairing code. The server is standard library only, so the pair comes from the
openssl binary; without one the phone listener is unavailable, never insecure."""
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import paths, tlscert  # noqa: E402


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="metachat-tls-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        # hermetic: no login shell; the process PATH decides where openssl is
        self._shell = os.environ.get("SHELL")
        os.environ["SHELL"] = str(self.state / "no-such-shell")
        paths.login_path.cache_clear()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._shell is None:
            os.environ.pop("SHELL", None)
        else:
            os.environ["SHELL"] = self._shell
        paths.login_path.cache_clear()

    def test_makes_one_pair_and_keeps_it(self):
        c = tlscert.ensure(self.state)
        self.assertEqual((c.cert, c.key), (self.state / "tls" / "cert.pem", self.state / "tls" / "key.pem"))
        self.assertTrue(c.cert.is_file() and c.key.is_file())
        self.assertEqual(c.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "tls").stat().st_mode & 0o777, 0o700)
        self.assertRegex(c.fingerprint, r"^[0-9a-f]{64}$")
        pem = c.cert.read_text()
        again = tlscert.ensure(self.state)
        self.assertEqual(again, c)
        self.assertEqual(c.cert.read_text(), pem)   # not regenerated

    def test_fingerprint_is_sha256_of_the_der(self):
        c = tlscert.ensure(self.state)
        der = ssl.PEM_cert_to_DER_cert(c.cert.read_text())
        self.assertEqual(hashlib.sha256(der).hexdigest(), c.fingerprint)
        self.assertEqual(tlscert.fingerprint_of(c.cert.read_text()), c.fingerprint)

    def test_the_key_is_p256(self):
        c = tlscert.ensure(self.state)
        out = subprocess.run([tlscert.openssl(), "ec", "-in", str(c.key), "-noout", "-text"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("prime256v1", out.stdout + out.stderr)
        info = ssl._ssl._test_decode_cert(str(c.cert))
        self.assertEqual(info["subject"], ((("commonName", "metabot"),),))
        days = (ssl.cert_time_to_seconds(info["notAfter"]) - ssl.cert_time_to_seconds(info["notBefore"])) / 86400
        self.assertGreaterEqual(days, 3649)

    def test_the_handshake_presents_the_pinned_fingerprint(self):
        """What the phone computes (sha256 of the leaf it is shown) is the f= value."""
        c = tlscert.ensure(self.state)
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(c.cert, c.key)
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        self.addCleanup(lsock.close)

        def serve():
            conn, _ = lsock.accept()
            try:
                with sctx.wrap_socket(conn, server_side=True) as s:
                    s.recv(1)
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cctx.check_hostname = False
        cctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", lsock.getsockname()[1]), timeout=10) as raw:
            with cctx.wrap_socket(raw, server_hostname="metabot") as s:
                seen = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
                s.sendall(b"x")
        th.join(10)
        self.assertEqual(seen, c.fingerprint)

    def test_a_corrupt_pair_is_replaced(self):
        c = tlscert.ensure(self.state)
        c.cert.write_text("not a certificate\n")
        again = tlscert.ensure(self.state)
        self.assertRegex(again.fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotEqual(again.fingerprint, c.fingerprint)
        self.assertEqual(again.key.stat().st_mode & 0o777, 0o600)
        # one file missing is the same as none
        again.key.unlink()
        third = tlscert.ensure(self.state)
        self.assertNotEqual(third.fingerprint, again.fingerprint)
        self.assertTrue(third.key.is_file())

    def test_no_openssl_is_unavailable_not_insecure(self):
        with mock.patch.object(tlscert, "openssl", return_value=None):
            with self.assertRaises(tlscert.Unavailable):
                tlscert.ensure(self.state)
        self.assertFalse((self.state / "tls" / "cert.pem").exists())


if __name__ == "__main__":
    unittest.main()
