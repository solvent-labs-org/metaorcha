"""C1: the phone listener's certificate.

One self-signed P-256 pair per host, kept under ``STATE/tls`` (``cert.pem``,
``key.pem`` 0600), made once and reused. The phone never trusts a CA: it pins
``sha256(DER of the leaf)``, which is the ``f=`` part of a pairing code and the
value ``fingerprint_of`` returns. The server is standard library only, so the
pair comes from the ``openssl`` binary (OpenSSL or LibreSSL); without one the
phone listener is unavailable, never plain HTTP.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths

DAYS = 3650            # ten years: the phone pins the fingerprint, nothing expires it early
SUBJECT = "/CN=metabot"
CURVE = "prime256v1"   # P-256: every Android and browser TLS stack accepts it
TIMEOUT = 30


class Unavailable(RuntimeError):
    """No openssl binary: the certificate cannot be made on this host."""


@dataclass(frozen=True)
class Cert:
    cert: Path
    key: Path
    fingerprint: str   # 64 lowercase hex characters


def openssl() -> str | None:
    return paths.find_bin("openssl")


def fingerprint_of(pem_text: str) -> str:
    """sha256 of the DER certificate, as the phone computes it. ValueError when not a certificate."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem_text)).hexdigest()


def _load(cert: Path, key: Path) -> Cert | None:
    try:
        fp = fingerprint_of(cert.read_text())
        key_text = key.read_text()
    except (OSError, ValueError):
        return None
    if "PRIVATE KEY" not in key_text:
        return None
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (OSError, ssl.SSLError):
        return None
    return Cert(cert, key, fp)


def _private_file(path: Path) -> None:
    """Create ``path`` empty with mode 0600; openssl then rewrites it in place."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable(f"openssl failed: {exc}") from exc


def ensure(state: Path) -> Cert:
    """The host's pair, made on first call. Raises Unavailable without openssl."""
    d = Path(state) / "tls"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    cert, key = d / "cert.pem", d / "key.pem"
    found = _load(cert, key)
    if found is not None:
        return found
    binary = openssl()
    if not binary:
        raise Unavailable("openssl not found")
    tmp_key, tmp_cert = d / "key.pem.new", d / "cert.pem.new"
    _private_file(tmp_key)
    _private_file(tmp_cert)
    try:
        _run([binary, "ecparam", "-name", CURVE, "-genkey", "-noout", "-out", str(tmp_key)])
        _run([binary, "req", "-new", "-x509", "-key", str(tmp_key), "-out", str(tmp_cert),
              "-days", str(DAYS), "-subj", SUBJECT, "-sha256"])
        os.chmod(tmp_key, 0o600)
        made = _load(tmp_cert, tmp_key)
        if made is None:
            raise Unavailable("openssl wrote an unusable pair")
        os.replace(tmp_key, key)
        os.replace(tmp_cert, cert)
    finally:
        for p in (tmp_key, tmp_cert):
            try:
                p.unlink()
            except OSError:
                pass
    return Cert(cert, key, made.fingerprint)
