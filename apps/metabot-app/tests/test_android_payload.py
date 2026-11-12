"""E1: the on-device payload's labelled pure-Python Ed25519 (the `cryptography`
stand-in, android/payload/crypto-pure-ed25519). RFC 8032 section 7.1 vectors pin
the math; the surface is exactly the eight names the SDK uses. The module is v0
only and carries its label: version marker, no spike wording."""
import importlib
import sys
import unittest
from pathlib import Path

PAYLOAD = Path(__file__).resolve().parents[1] / "android" / "payload" / "crypto-pure-ed25519"

# RFC 8032, section 7.1, TEST 1 (empty message) and TEST 2 (one octet)
VECTOR_1 = {
    "secret": "9d61b19deffd5a60ba844af492ec2cc4" "4449c5697b326919703bac031cae7f60",
    "public": "d75a980182b10ab7d54bfed3c964073a" "0ee172f3daa62325af021a68f707511a",
    "message": "",
    "signature": ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                  "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
}
VECTOR_2 = {
    "secret": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
    "public": "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    "message": "72",
    "signature": ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                  "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
}


def setUpModule():
    # the payload's cryptography must win over an installed one, and over any
    # other test module's import of the real package in this process
    for name in [n for n in sys.modules if n == "cryptography" or n.startswith("cryptography.")]:
        del sys.modules[name]
    sys.path.insert(0, str(PAYLOAD))


def tearDownModule():
    sys.path.remove(str(PAYLOAD))
    for name in [n for n in sys.modules if n == "cryptography" or n.startswith("cryptography.")]:
        del sys.modules[name]


class PureEd25519Tests(unittest.TestCase):
    def setUp(self):
        self.ed = importlib.import_module("cryptography.hazmat.primitives.asymmetric.ed25519")
        self.exc = importlib.import_module("cryptography.exceptions")

    def test_the_package_is_labelled_for_v0(self):
        pkg = importlib.import_module("cryptography")
        self.assertEqual(pkg.__version__, "0.0.0+metaorcha-pure-ed25519")
        self.assertFalse(hasattr(pkg, "SPIKE_SHIM"))
        notice = (PAYLOAD.parent / "NOTICE-crypto-pure-ed25519.txt").read_text()
        self.assertIn("NOT the cryptography project", " ".join(notice.split()))

    def test_rfc_8032_vectors(self):
        for v in (VECTOR_1, VECTOR_2):
            secret = bytes.fromhex(v["secret"])
            pub = bytes.fromhex(v["public"])
            sig = bytes.fromhex(v["signature"])
            msg = bytes.fromhex(v["message"])
            key = self.ed.Ed25519PrivateKey.from_private_bytes(secret)
            self.assertEqual(key.private_bytes_raw(), secret)
            self.assertEqual(key.public_key().public_bytes_raw(), pub)
            self.assertEqual(key.sign(msg), sig)
            self.ed.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            with self.assertRaises(self.exc.InvalidSignature):
                self.ed.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg + b"x")

    def test_generate_roundtrip(self):
        key = self.ed.Ed25519PrivateKey.generate()
        sig = key.sign(b"a receipt")
        key.public_key().verify(sig, b"a receipt")
        bad = bytearray(sig)
        bad[0] ^= 1
        with self.assertRaises(self.exc.InvalidSignature):
            key.public_key().verify(bytes(bad), b"a receipt")

    def test_exactly_the_sdks_eight_names(self):
        priv, pub = self.ed.Ed25519PrivateKey, self.ed.Ed25519PublicKey
        for name in ("generate", "from_private_bytes", "sign", "public_key", "private_bytes_raw"):
            self.assertTrue(callable(getattr(priv, name, None)), name)
        for name in ("from_public_bytes", "verify", "public_bytes_raw"):
            self.assertTrue(callable(getattr(pub, name, None)), name)
        self.assertTrue(issubclass(self.exc.InvalidSignature, Exception))
        with self.assertRaises(ValueError):
            priv.from_private_bytes(b"short")
        with self.assertRaises(ValueError):
            pub.from_public_bytes(b"short")


if __name__ == "__main__":
    unittest.main()
