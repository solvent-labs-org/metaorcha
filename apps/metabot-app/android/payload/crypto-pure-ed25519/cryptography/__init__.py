"""MetaBot on-device pure-Python Ed25519, standing in for the `cryptography` package.

Why this exists (E1, v0 only): no Android arm64 wheel of `cryptography` exists
(BeeWare index: iOS only; PyPI: none), and building one needs Rust + the NDK,
which is not approved. The MetaBot SDK uses exactly one primitive, Ed25519
(eight names; see android/payload/NOTICE-crypto-pure-ed25519.txt), so the APK
ships this package under the `cryptography` name over a pure-Python RFC 8032
implementation (`hazmat.primitives._ed25519_pure`). It is slow and not
constant-time. It produces and verifies real, interoperable Ed25519 signatures
(proven against `cryptography` 50.0.0 on RFC vectors and a PC-side receipt
cross-check, 2026-11-10). It is replaced by a cross-built `cryptography` the
day the NDK + Rust target are approved. Never ship it anywhere but the
on-device APK payload.
"""
__version__ = "0.0.0+metaorcha-pure-ed25519"
