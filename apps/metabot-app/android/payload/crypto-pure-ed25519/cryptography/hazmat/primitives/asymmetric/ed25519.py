"""The Ed25519 surface the MetaBot SDK uses, over _ed25519_pure (E1 v0; see the
package docstring: a labelled stand-in for `cryptography`, on-device only)."""
import os
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import _ed25519_pure as _e

class Ed25519PublicKey:
    def __init__(self, raw):
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
            raise ValueError("An Ed25519 public key is 32 bytes long")
        self._raw = bytes(raw)
    @classmethod
    def from_public_bytes(cls, data):
        key = cls(data)
        if _e._decompress(key._raw) is None:
            raise ValueError("Invalid Ed25519 public key")
        return key
    def public_bytes_raw(self):
        return self._raw
    def verify(self, signature, data):
        if not _e.verify(self._raw, bytes(data), bytes(signature)):
            raise InvalidSignature()
    def __eq__(self, other):
        return isinstance(other, Ed25519PublicKey) and other._raw == self._raw

class Ed25519PrivateKey:
    def __init__(self, seed):
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise ValueError("An Ed25519 private key is 32 bytes long")
        self._seed = bytes(seed)
    @classmethod
    def generate(cls):
        return cls(os.urandom(32))
    @classmethod
    def from_private_bytes(cls, data):
        return cls(data)
    def private_bytes_raw(self):
        return self._seed
    def public_key(self):
        return Ed25519PublicKey(_e.public_key(self._seed))
    def sign(self, data):
        return _e.sign(self._seed, bytes(data))
