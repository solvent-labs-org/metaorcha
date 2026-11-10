#!/usr/bin/env python3
"""Generate the tablet-verifier parity corpus: tests/fixtures/parity/cases.json.

Run it under MetaBot's interpreter, which has ``cryptography``::

    <python with orcha-sdk> tests/parity.py [--no-cli] [--probe]

What it does
------------
* Imports the sdk (``emerge.run_attestation``, sdk 0.2.2) from
  ``<repo>/sdk/src`` and uses it as the oracle. Every expected
  verdict in the corpus is computed by the sdk, never typed by hand.
* Builds envelopes with a throwaway Ed25519 key that lives only in this
  process. Mutants are re-signed after the mutation, so a case that is meant to
  fail at ``steps_root`` or ``signature`` reaches that check instead of dying
  at ``schema``.
* Maps the CLI's "no verdict" set the way ``emerge.cli.cmd_verify`` does:
  ``bytes.decode("utf-8")`` then ``json.loads`` then ``isinstance(x, dict)``.
  Anything that fails there is ``problem: "malformed"``. The real CLI exits 2
  for the JSON errors; for invalid UTF-8, a >4300-digit integer and nesting
  past CPython's C recursion limit it dies with a traceback (exit 1, no JSON)
  because ``cmd_verify`` only catches ``OSError`` and ``JSONDecodeError``.
  Those cases carry ``sdk_exception`` so a reader can see which happened.
* Unless ``--no-cli`` is given, also runs the real ``orcha verify --json`` on
  every case in a temp dir (HOME and METABOT_HOME pinned there) and checks it
  agrees with the in-process oracle. A disagreement is printed and makes the
  exit status non-zero; the corpus is still written.

Nothing here reads ``~/.metabot``, ``~/.orcha`` or the real state dir, and no
desk, firm or person name appears in any fixture: desks are ``desk-a`` etc.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import secrets
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

SDK_SRC = Path(os.environ.get("MBVERIFY_SDK_SRC", Path(__file__).resolve().parents[3] / "sdk/src"))
ORCHA_BIN = Path(os.environ.get("MBVERIFY_ORCHA_BIN", shutil.which("orcha") or "orcha"))
HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "src" / "metabot_app" / "selftest" / "fixtures" / "parity" / "cases.json"
GOLDEN_LOCAL = HERE / "fixtures" / "golden.json"
GOLDEN_SDK = SDK_SRC.parent.parent / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"

sys.path.insert(0, str(SDK_SRC))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import emerge  # noqa: E402
from emerge import run_attestation as ra  # noqa: E402
from emerge import record as sdk_record  # noqa: E402

# the sdk logs a traceback for the two TypeError cases (unhashable `result`); that is the
# behaviour under test, not noise worth printing
import logging  # noqa: E402

logging.getLogger("emerge").setLevel(logging.CRITICAL)

# ───────────────────────── pure-Python ed25519 (for crafting only) ──────────────────────────
# Used to build edge-case keys and signatures (small-order keys, non-canonical
# encodings, S >= L, torsion). Verdicts still come from the sdk; this code only
# produces bytes to feed it.

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, -1, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sqrt_ratio(u: int, v: int) -> int | None:
    x = (u * pow(v, 3, P) * pow(u * pow(v, 7, P), (P - 5) // 8, P)) % P
    if (v * x * x - u) % P == 0:
        return x
    if (v * x * x + u) % P == 0:
        return (x * SQRT_M1) % P
    return None


def pt_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % P
    B = (y1 + x1) * (y2 + x2) % P
    C = 2 * t1 * t2 * D % P
    Dd = 2 * z1 * z2 % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def pt_mul(p, n: int):
    acc = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            acc = pt_add(acc, p)
        p = pt_add(p, p)
        n >>= 1
    return acc


def pt_neg(p):
    x, y, z, t = p
    return ((-x) % P, y, z, (-t) % P)


def pt_affine(p):
    x, y, z, _ = p
    zi = pow(z, -1, P)
    return (x * zi % P, y * zi % P)


def pt_encode(p) -> bytes:
    """Canonical RFC 8032 encoding: y little-endian, sign of x in the top bit."""
    x, y = pt_affine(p)
    b = bytearray(y.to_bytes(32, "little"))
    b[31] |= (x & 1) << 7
    return bytes(b)


def pt_decode(b: bytes):
    """Canonical decode (RFC 8032 rules); returns None when it is not a point."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    sign = b[31] >> 7
    if y >= P:
        return None
    u, v = (y * y - 1) % P, (D * y * y + 1) % P
    x = _sqrt_ratio(u, v)
    if x is None:
        return None
    if x == 0 and sign:
        return None
    if (x & 1) != sign:
        x = (-x) % P
    return (x, y, 1, x * y % P)


def pt_eq(a, b) -> bool:
    return pt_affine(a) == pt_affine(b)


def pt_is_zero(p) -> bool:
    return pt_affine(p) == (0, 1)


BASE = pt_decode(bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666"))
assert BASE is not None and pt_is_zero(pt_mul(BASE, L))

# The eight points of the torsion subgroup, as @noble/curves lists them
# (ED25519_TORSION_SUBGROUP). Orders: 1, 8, 4, 8, 2, 8, 4, 8.
TORSION_HEX = [
    "0100000000000000000000000000000000000000000000000000000000000000",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
]


def pt_order_small(p) -> int:
    for n in (1, 2, 4, 8):
        if pt_is_zero(pt_mul(p, n)):
            return n
    raise ValueError("not a small-order point")


def sha512(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for part in parts:
        h.update(part)
    return h.digest()


def craft_small_order_sig(a_bytes: bytes, a_point, msg: bytes, s_start: int = 1) -> bytes:
    """(R, S) that passes OpenSSL-style verification for a small-order key.

    With ``n·A = O`` the term ``k·A`` only depends on ``k mod n``, so guess
    ``j = k mod n``, set ``R = S·B - j·A`` and keep going until the guess
    agrees with the hash. ``a_bytes`` is the exact key encoding that will sit
    in the envelope (it may be non-canonical); the hash covers those bytes.
    """
    n = pt_order_small(a_point)
    s = s_start
    while True:
        sb = pt_mul(BASE, s)
        for j in range(n):
            r_point = pt_add(sb, pt_neg(pt_mul(a_point, j))) if j else sb
            r_bytes = pt_encode(r_point)
            k = int.from_bytes(sha512(r_bytes, a_bytes, msg), "little") % L
            if k % n == j:
                return r_bytes + s.to_bytes(32, "little")
        s += 1


def scalar_of(key: Ed25519PrivateKey) -> tuple[int, bytes]:
    h = sha512(key.private_bytes_raw())
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


# ───────────────────────────── envelope building ──────────────────────────────

KEY = Ed25519PrivateKey.generate()
KEY2 = Ed25519PrivateKey.generate()
PUB_RAW = KEY.public_key().public_bytes_raw()
PUB_B64 = base64.b64encode(PUB_RAW).decode("ascii")
PUB2_B64 = base64.b64encode(KEY2.public_key().public_bytes_raw()).decode("ascii")
SIGNER_DID = "did:orcha:agent:key-" + hashlib.sha256(PUB_RAW).hexdigest()[:16]
HEX = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731


class Raw(str):
    """A leaf that is written into the JSON text verbatim (``1.0``, ``NaN``, ``-0``…).

    Rendered through a one-off token so ``json.dumps`` cannot touch it.
    """

    def __new__(cls, text: str):
        obj = super().__new__(cls, text)
        obj.token = "@@RAW:" + secrets.token_hex(8) + "@@"
        return obj


def _tokens(obj, acc: dict[str, str]) -> Any:
    if isinstance(obj, Raw):
        acc[obj.token] = str(obj)
        return obj.token
    if isinstance(obj, dict):
        return {k: _tokens(v, acc) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_tokens(v, acc) for v in obj]
    return obj


def render(obj, **dump_kw) -> str:
    acc: dict[str, str] = {}
    text = json.dumps(_tokens(obj, acc), **dump_kw)
    for token, raw in acc.items():
        text = text.replace(json.dumps(token), raw)
    return text


def view(obj) -> Any:
    """Python's reading of an envelope that may hold Raw leaves."""
    return json.loads(render(obj))


def step(i: int, **over) -> dict:
    s = {
        "seq": i,
        "call_id": f"call-{i:04x}",
        "tool": ["read_file", "bash", "search_docs"][i % 3],
        "args_hash": HEX(f"args-{i}"),
        "output_hash": HEX(f"out-{i}"),
        "success": i % 4 != 3,
        "latency_ms": 10 * i + 7,
    }
    if i % 2:
        s["cdv_bp"] = (i * 137) % 1001
    s.update(over)
    return s


def base_env(n_steps: int = 3, **over) -> dict:
    env = {
        "format": ra.RUN_ATTESTATION_FORMAT,
        "run_id": "parity-run",
        "agent_dids": ["did:orcha:agent:desk-a"],
        "charter_hash": None,
        "policy_version": "parity/1",
        "steps": [step(i) for i in range(n_steps)],
        "steps_root": "",
        "steps_merkle_root": "",
        "verdicts": [
            {"check": "authorized_scope", "result": "pass"},
            {"check": "engine", "result": "warn", "detail": "desk-a ran engine x"},
        ],
        "started_at": "2026-11-08T10:00:00Z",
        "finished_at": "2026-11-08T10:00:05Z",
        "signer": {"did": SIGNER_DID, "public_key_b64": PUB_B64},
    }
    env.update(over)
    return env


def seal(env_unsigned: dict, key: Ed25519PrivateKey = KEY, fix_roots: bool = True) -> dict:
    """Recompute both roots (unless told not to) and sign, over Python's view of the leaves."""
    env = copy.deepcopy(env_unsigned)
    env.pop("signature", None)
    if fix_roots and isinstance(env.get("steps"), list):
        steps_view = view(env)["steps"]
        env["steps_root"] = ra.compute_steps_root(steps_view)
        env["steps_merkle_root"] = ra.compute_steps_merkle_root(steps_view)
    digest = ra.compute_envelope_digest(view(env))
    env["signature"] = base64.b64encode(key.sign(digest.encode("utf-8"))).decode("ascii")
    return env


def valid_env(n_steps: int = 3, **over) -> dict:
    return seal(base_env(n_steps, **over))


def with_sig(env_unsigned: dict, raw_sig: bytes) -> dict:
    env = copy.deepcopy(env_unsigned)
    env.pop("signature", None)
    env["signature"] = base64.b64encode(raw_sig).decode("ascii")
    return env


def digest_msg(env_unsigned: dict) -> bytes:
    env = copy.deepcopy(env_unsigned)
    env.pop("signature", None)
    return ra.compute_envelope_digest(view(env)).encode("utf-8")


# ─────────────────────────────── the oracle ───────────────────────────────────

CHECK_KEYS = ("schema", "steps_root", "steps_merkle_root", "signature")


def malformed(exc_name: str) -> dict:
    return {
        "expect": {
            "valid": False,
            "verdict": "malformed",
            "problem": "malformed",
            "checks": {k: False for k in CHECK_KEYS},
            "digest": None,
            "run_id": None,
            "signer_did": None,
            "step_count": None,
        },
        "sdk_exception": exc_name,
    }


def expect_for(data: bytes) -> dict:
    """The sdk's answer for these bytes, read the way ``emerge.cli.cmd_verify`` reads a file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return malformed("UnicodeDecodeError")
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return malformed("JSONDecodeError")
    except Exception as exc:  # RecursionError, ValueError (int digits) — the CLI tracebacks here
        return malformed(type(exc).__name__)
    if not isinstance(envelope, dict):
        return malformed("not_object")
    v = ra.verify_run_attestation(envelope)
    digest = ra.compute_envelope_digest(envelope) if v.checks["steps_merkle_root"] else None
    return {
        "expect": {
            "valid": v.valid,
            "verdict": v.verdict,
            "problem": None,
            "checks": {k: v.checks[k] for k in CHECK_KEYS},
            "digest": digest,
            "run_id": v.run_id,
            "signer_did": v.signer_did,
            "step_count": v.step_count,
        }
    }


def cli_for(data: bytes, workdir: Path) -> dict:
    """``orcha verify --json`` on these bytes; HOME and METABOT_HOME are the temp dir."""
    target = workdir / "case.json"
    target.write_bytes(data)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(workdir), "METABOT_HOME": str(workdir / "mh"),
           "LANG": os.environ.get("LANG", "C.UTF-8"), "LC_ALL": os.environ.get("LC_ALL", "")}
    env = {k: v for k, v in env.items() if v}
    proc = subprocess.run([str(ORCHA_BIN), "verify", "--json", str(target)], cwd=str(workdir), env=env,
                          capture_output=True, timeout=60)
    out: dict[str, Any] = {"exit": proc.returncode, "json": None}
    if proc.stdout.strip():
        try:
            out["json"] = json.loads(proc.stdout.decode("utf-8"))
        except ValueError:
            out["json"] = None
    err_lines = [ln for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    out["stderr"] = err_lines[-1][:200] if err_lines else ""
    return out


def cli_agrees(expect: dict, cli: dict) -> bool:
    if cli["exit"] == 2:
        return expect["problem"] == "malformed"
    if cli["json"] is None:
        # a traceback: the CLI produced no verdict at all
        return expect["problem"] == "malformed"
    j = cli["json"]
    return (
        expect["problem"] is None
        and j["valid"] == expect["valid"]
        and j["verdict"] == expect["verdict"]
        and j["checks"] == expect["checks"]
        and j["run_id"] == expect["run_id"]
        and j["signer_did"] == expect["signer_did"]
        and j["step_count"] == expect["step_count"]
        and cli["exit"] == (0 if expect["valid"] else 1)
    )


# ──────────────────────────────── the cases ───────────────────────────────────

CASES: list[dict] = []
_names: set[str] = set()


def add(name: str, data: bytes | str | dict, note: str = "", **dump_kw) -> None:
    if name in _names:
        raise SystemExit(f"duplicate case name {name}")
    _names.add(name)
    if isinstance(data, dict):
        data = render(data, **dump_kw)
    if isinstance(data, str):
        data = data.encode("utf-8")
    CASES.append({"name": name, "bytes": data, "note": note})


def load_golden() -> tuple[dict, str]:
    """The golden vector and a label for it. The corpus records the label, never a path."""
    for path, label in ((GOLDEN_LOCAL, "tests/fixtures/golden.json"),
                        (GOLDEN_SDK, "docs/spec/test-vectors/run-attestation-golden.json")):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), label
    raise SystemExit("golden vector not found")


def b64_nonstrict_variant(b64: str) -> str:
    """Change discarded low bits of the last data char: same bytes under Python's decoder."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    data = b64.rstrip("=")
    pads = len(b64) - len(data)
    last = alphabet.index(data[-1])
    if pads == 1:      # 4 data bits, 2 discarded
        new = alphabet[(last & 0b111100) | ((last & 0b11) ^ 0b01)]
    elif pads == 2:    # 2 data bits, 4 discarded
        new = alphabet[(last & 0b110000) | ((last & 0b1111) ^ 0b0101)]
    else:
        raise ValueError("needs padding")
    return data[:-1] + new + "=" * pads


def build_cases(golden: dict) -> None:
    # 1. golden vectors, as shipped and re-rendered
    add("golden.valid", json.dumps(golden["valid"], indent=2), "spec vector, pretty")
    add("golden.valid.compact", json.dumps(golden["valid"], separators=(",", ":")), "spec vector, compact")
    add("golden.tampered", json.dumps(golden["tampered"], indent=2), "latency_ms 132 -> 133; dies at steps_root")

    # 2. the sdk's own producer path
    produced = sdk_record.sign_run_envelope(
        sdk_record.build_run_envelope(
            run_id="parity-producer",
            agent_dids=["did:orcha:agent:desk-b"],
            charter_hash=None,
            policy_version="parity/1",
            steps=[{"call_id": "c1", "tool": "bash", "args": {"cmd": "ls"}, "output": "ok\n", "success": True,
                    "latency_ms": 12},
                   {"call_id": "c2", "tool": "read_file", "args": {"path": "x"}, "output": {"n": 1}, "success": False,
                    "latency_ms": 3, "cdv_bp": 5}],
            verdicts=[{"check": "a", "result": "pass", "detail": ""}],
            started_at="2026-11-08T09:00:00Z",
            finished_at="2026-11-08T09:00:01Z",
            signer_did=SIGNER_DID,
            public_key_b64=PUB_B64,
        ),
        KEY,
    )
    add("sdk.producer_sealed", produced, "built by emerge.record.build_run_envelope + sign_run_envelope")

    # 3. Merkle / chain sizes
    for n in (0, 1, 2, 3, 4, 5, 7, 8, 9, 16, 17):
        add(f"merkle.size_{n}", valid_env(n), f"{n} steps, valid")
    for n in (3, 8):
        e = base_env(n)
        e = seal(e)
        e["steps_root"] = HEX("wrong-root")
        add(f"merkle.size_{n}.bad_steps_root", seal(e, fix_roots=False), "re-signed; dies at steps_root")
        e = seal(base_env(n))
        e["steps_merkle_root"] = HEX("wrong-merkle")
        add(f"merkle.size_{n}.bad_merkle_root", seal(e, fix_roots=False), "re-signed; dies at steps_merkle_root")
        e = seal(base_env(n))
        e["steps_root"], e["steps_merkle_root"] = e["steps_merkle_root"], e["steps_root"]
        add(f"merkle.size_{n}.roots_swapped", seal(e, fix_roots=False), "re-signed; dies at steps_root")

    # 4. check order mutants (each re-signed so the intended check is reached)
    e = valid_env()
    sig = bytearray(base64.b64decode(e["signature"]))
    sig[5] ^= 0x01
    add("order.bad_signature_bit", with_sig(e, bytes(sig)), "one bit of R flipped; dies at signature")
    sig = bytearray(base64.b64decode(e["signature"]))
    sig[40] ^= 0x01
    add("order.bad_signature_s_bit", with_sig(e, bytes(sig)), "one bit of S flipped; dies at signature")
    add("order.signed_by_other_key", seal(base_env(), KEY2), "signer names KEY, signature by KEY2")
    e = base_env()
    e["signer"]["public_key_b64"] = PUB2_B64
    add("order.other_public_key", seal(e, KEY), "signer names KEY2, signature by KEY")
    e = valid_env()
    e["steps"][1]["latency_ms"] += 1
    add("order.step_tampered_after_seal", e, "steps_root mismatch, like golden.tampered")
    e = valid_env()
    e["steps"][0]["success"] = False
    add("order.step_bool_tampered", e, "steps_root mismatch")
    e = valid_env()
    e["run_id"] = "parity-run-2"
    add("order.envelope_field_tampered", e, "roots fine, digest differs; dies at signature")
    e = valid_env()
    e["steps_root"] = e["steps_root"].upper()
    add("order.steps_root_uppercase", seal(e, fix_roots=False), "HEX64 is lowercase; dies at schema")
    e = valid_env()
    e["steps_root"] = e["steps_root"] + "\n"
    add("order.steps_root_trailing_newline", seal(e, fix_roots=False),
        "Python `$` lets the newline through the schema; the compare then fails at steps_root")
    e = valid_env()
    e["steps_merkle_root"] = e["steps_merkle_root"] + "\n"
    add("order.merkle_root_trailing_newline", seal(e, fix_roots=False), "schema ok, steps_root ok, merkle fails")
    e = valid_env()
    e["steps_root"] = e["steps_root"][:63]
    add("order.steps_root_63_chars", seal(e, fix_roots=False), "dies at schema")
    e = valid_env()
    e["signature"] = e["signature"][:-4] + "AAAA"
    add("order.signature_tail_replaced", e, "dies at signature")

    # 5. envelope field sweep
    for field in sorted(ra._ENVELOPE_FIELDS):
        e = base_env()
        e = seal(e)
        del e[field]
        add(f"schema.missing.{field}", e, "closed field set")
    e = seal(base_env())
    e["extra"] = 1
    add("schema.extra_field", seal(e, fix_roots=False), "closed field set")
    add("schema.format.wrong_string", seal(base_env(format="orcha.run-attestation/v2")))
    add("schema.format.int", seal(base_env(format=1)))
    add("schema.format.null", seal(base_env(format=None)))

    # run_id and the printable-ASCII rule
    add("schema.run_id.empty", seal(base_env(run_id="")))
    add("schema.run_id.int", seal(base_env(run_id=7)))
    add("schema.run_id.list", seal(base_env(run_id=["a"])))
    add("schema.run_id.tab", seal(base_env(run_id="a\tb")))
    add("schema.run_id.del_char", seal(base_env(run_id="a\x7fb")), "0x7f is outside [\\x20-\\x7e]")
    add("schema.run_id.non_ascii", seal(base_env(run_id="café")))
    add("schema.run_id.emoji_raw", seal(base_env(run_id="run-\U0001f600")), "raw 4-byte UTF-8 in the file")
    add("schema.run_id.trailing_newline.valid", seal(base_env(run_id="parity-run\n")),
        "Python `$` matches before one trailing newline; canonical bytes carry \\n")
    add("schema.run_id.two_trailing_newlines", seal(base_env(run_id="parity-run\n\n")))
    add("schema.run_id.leading_newline", seal(base_env(run_id="\nparity-run")))
    add("schema.run_id.trailing_cr", seal(base_env(run_id="parity-run\r")), "only \\n is forgiven by `$`")
    add("schema.run_id.slash.valid", seal(base_env(run_id="a/b/c")), "`/` is not escaped in canonical form")
    add("schema.run_id.quote_backslash.valid", seal(base_env(run_id='say "hi" \\ ok')), "escapes in canonical form")
    e = seal(base_env(run_id="Abc"))
    text = render(e, indent=1).replace('"run_id": "Abc"', '"run_id": "\\u0041b\\u0063"')
    add("schema.run_id.unicode_escapes_ascii.valid", text, "\\u0041 escapes decode to ASCII; still valid")
    e = seal(base_env(run_id="parity\n"))
    text = render(e).replace('"run_id": "parity\\n"', '"run_id": "parity\\u000a"')
    add("schema.run_id.escaped_newline_as_u000a.valid", text, "same string, different escape spelling")

    # agent_dids
    add("schema.agent_dids.not_list", seal(base_env(agent_dids="did:orcha:agent:x")))
    add("schema.agent_dids.empty.valid", seal(base_env(agent_dids=[])))
    add("schema.agent_dids.int_item", seal(base_env(agent_dids=[123])))
    add("schema.agent_dids.uppercase_method", seal(base_env(agent_dids=["did:ORCHA:agent:x"])))
    add("schema.agent_dids.space_in_id.valid", seal(base_env(agent_dids=["did:orcha:agent:desk a"])))
    add("schema.agent_dids.empty_id", seal(base_env(agent_dids=["did:orcha:"])))
    add("schema.agent_dids.no_method", seal(base_env(agent_dids=["did::x"])))
    add("schema.agent_dids.trailing_newline.valid", seal(base_env(agent_dids=["did:orcha:agent:x\n"])))
    add("schema.agent_dids.two.valid", seal(base_env(agent_dids=["did:orcha:agent:a", "did:web:example"])))

    # charter_hash
    add("schema.charter_hash.hex.valid", seal(base_env(charter_hash=HEX("charter"))))
    add("schema.charter_hash.uppercase", seal(base_env(charter_hash=HEX("charter").upper())))
    add("schema.charter_hash.63", seal(base_env(charter_hash=HEX("charter")[:63])))
    add("schema.charter_hash.float", seal(base_env(charter_hash=Raw("1.5"))))
    add("schema.charter_hash.true", seal(base_env(charter_hash=True)))
    add("schema.charter_hash.trailing_newline.valid", seal(base_env(charter_hash=HEX("charter") + "\n")))

    # policy_version
    add("schema.policy_version.empty", seal(base_env(policy_version="")))
    add("schema.policy_version.int", seal(base_env(policy_version=1)))

    # timestamps and the \d rule
    add("schema.started_at.int", seal(base_env(started_at=20261108)))
    add("schema.started_at.null", seal(base_env(started_at=None)))
    add("schema.started_at.no_z", seal(base_env(started_at="2026-11-08T10:00:00")))
    add("schema.started_at.offset", seal(base_env(started_at="2026-11-08T10:00:00+00:00")))
    add("schema.started_at.fraction", seal(base_env(started_at="2026-11-08T10:00:00.000Z")))
    add("schema.started_at.leading_space", seal(base_env(started_at=" 2026-11-08T10:00:00Z")))
    add("schema.finished_at.trailing_newline.valid", seal(base_env(finished_at="2026-11-08T10:00:05Z\n")))
    add("schema.started_at.arabic_indic_digits.valid",
        seal(base_env(started_at="٢٠٢٦-١١-٠٨T١٠:٠٠:٠٠Z")),
        "Python `\\d` is Unicode Nd; canonical form escapes them as lower-case \\u0662…")
    add("schema.started_at.fullwidth_digits.valid",
        seal(base_env(started_at="２０２６-11-08T10:00:00Z")), "U+FF10..FF19 are Nd")
    add("schema.started_at.math_bold_digits.valid",
        seal(base_env(started_at="\U0001d7d0\U0001d7ce\U0001d7d0\U0001d7d4-11-08T10:00:00Z")),
        "astral Nd digits; canonical form uses surrogate-pair escapes \\ud835\\udfd0")
    add("schema.started_at.garay_digit_unicode16", seal(base_env(started_at="\U00010d40026-11-08T10:00:00Z")),
        "U+10D40 is Nd since Unicode 16; Python 3.12 (Unicode 15) says no")
    add("schema.started_at.superscript_two", seal(base_env(started_at="²026-11-08T10:00:00Z")),
        "U+00B2 is category No, not Nd")
    add("schema.started_at.devanagari_digits.valid",
        seal(base_env(started_at="२०२६-११-०८T१०:००:००Z")))

    # steps container
    add("schema.steps.dict", seal(base_env(steps={"seq": 0})))
    add("schema.steps.null", seal(base_env(steps=None)), "step_count is null too")
    add("schema.steps.string", seal(base_env(steps="none")))
    add("schema.steps.item_not_dict", seal(base_env(steps=[1])))
    add("schema.steps.item_null", seal(base_env(steps=[None])))

    # step fields
    def step_case(name: str, idx: int, note: str = "", **over) -> None:
        e = base_env(3)
        e["steps"][idx].update(over)
        add(name, seal(e), note)

    def step_del(name: str, idx: int, key: str) -> None:
        e = base_env(3)
        del e["steps"][idx][key]
        add(name, seal(e), "step required set")

    for key in sorted(ra._STEP_REQUIRED):
        step_del(f"schema.step.missing.{key}", 1, key)
    step_case("schema.step.extra_key", 1, "closed set", x=1)
    step_case("schema.step.seq_wrong", 1, seq=2)
    step_case("schema.step.seq_true_at_1", 1, "True == 1 in Python but _is_int rejects bool", seq=True)
    step_case("schema.step.seq_false_at_0", 0, seq=False)
    step_case("schema.step.seq_1_0", 1, "float literal", seq=Raw("1.0"))
    step_case("schema.step.seq_1e0", 1, "float literal", seq=Raw("1e0"))
    step_case("schema.step.seq_big", 1, seq=2**70)
    step_case("schema.step.seq_neg_zero.valid", 0, "-0 parses to int 0; canonical form prints 0", seq=Raw("-0"))
    step_case("schema.step.seq_string", 1, seq="1")
    step_case("schema.step.cdv_bp.0.valid", 0, cdv_bp=0)
    step_case("schema.step.cdv_bp.1000.valid", 0, cdv_bp=1000)
    step_case("schema.step.cdv_bp.1001", 0, cdv_bp=1001)
    step_case("schema.step.cdv_bp.neg", 0, cdv_bp=-1)
    step_case("schema.step.cdv_bp.true", 0, cdv_bp=True)
    step_case("schema.step.cdv_bp.1_0", 0, cdv_bp=Raw("1.0"))
    step_case("schema.step.cdv_bp.big", 0, cdv_bp=2**70)
    step_case("schema.step.cdv_bp.null", 0, cdv_bp=None)
    step_case("schema.step.latency.neg", 2, latency_ms=-1)
    step_case("schema.step.latency.neg_zero.valid", 2, "-0 -> 0", latency_ms=Raw("-0"))
    step_case("schema.step.latency.zero.valid", 2, latency_ms=0)
    step_case("schema.step.latency.big.valid", 2, "2**70 is an int: exact digits in canonical form", latency_ms=2**70)
    step_case("schema.step.latency.4300_digits.valid", 2, "CPython's int() limit is 4300 digits; this is at it",
              latency_ms=int("9" * 4300))
    e = base_env(3)
    e["steps"][2]["latency_ms"] = 424242
    text = render(seal(e)).replace('"latency_ms": 424242', '"latency_ms": ' + "1" * 4301)
    add("schema.step.latency.4301_digits", text,
        "json.loads raises ValueError (not JSONDecodeError): the CLI tracebacks with no verdict")
    step_case("schema.step.latency.1_0", 2, latency_ms=Raw("1.0"))
    step_case("schema.step.latency.1e0", 2, latency_ms=Raw("1e0"))
    step_case("schema.step.latency.1E5", 2, latency_ms=Raw("1E5"))
    step_case("schema.step.latency.neg_0_0", 2, latency_ms=Raw("-0.0"))
    step_case("schema.step.latency.nan", 2, "NaN parses (Python json) then fails schema", latency_ms=Raw("NaN"))
    step_case("schema.step.latency.infinity", 2, latency_ms=Raw("Infinity"))
    step_case("schema.step.latency.neg_infinity", 2, latency_ms=Raw("-Infinity"))
    step_case("schema.step.latency.true", 2, latency_ms=True)
    step_case("schema.step.latency.string", 2, latency_ms="5")
    step_case("schema.step.latency.null", 2, latency_ms=None)
    step_case("schema.step.success.int", 0, success=1)
    step_case("schema.step.success.string", 0, success="true")
    step_case("schema.step.success.null", 0, success=None)
    step_case("schema.step.args_hash.uppercase", 0, args_hash=HEX("a").upper())
    step_case("schema.step.args_hash.trailing_newline.valid", 0,
              "schema forgives the newline; it is part of the hashed step bytes", args_hash=HEX("a") + "\n")
    step_case("schema.step.output_hash.short", 0, output_hash=HEX("a")[:10])
    step_case("schema.step.output_hash.int", 0, output_hash=5)
    step_case("schema.step.tool.empty", 0, tool="")
    step_case("schema.step.tool.non_ascii", 0, tool="bäsh")
    step_case("schema.step.call_id.control_escape", 0, "\\u0001 parses, then fails printable ASCII",
              call_id=Raw('"c\\u0001"'))
    step_case("schema.step.call_id.lone_surrogate_escape", 0, call_id=Raw('"c\\ud800"'))
    step_case("schema.step.call_id.pair_escape", 0, call_id=Raw('"c\\ud83d\\ude00"'))
    step_case("schema.step.call_id.int", 0, call_id=1)

    # verdicts
    add("schema.verdicts.dict", seal(base_env(verdicts={"check": "a"})))
    add("schema.verdicts.empty.valid", seal(base_env(verdicts=[])))
    add("schema.verdicts.item_not_dict", seal(base_env(verdicts=["pass"])))
    add("schema.verdicts.result_maybe", seal(base_env(verdicts=[{"check": "a", "result": "maybe"}])))
    add("schema.verdicts.result_fail.valid", seal(base_env(verdicts=[{"check": "a", "result": "fail"}])))
    add("schema.verdicts.result_true", seal(base_env(verdicts=[{"check": "a", "result": True}])))
    add("schema.verdicts.result_list", seal(base_env(verdicts=[{"check": "a", "result": ["pass"]}])),
        "unhashable in `result in frozenset` -> TypeError -> the sdk's catch-all -> invalid")
    add("schema.verdicts.result_dict", seal(base_env(verdicts=[{"check": "a", "result": {"a": 1}}])))
    add("schema.verdicts.result_null", seal(base_env(verdicts=[{"check": "a", "result": None}])))
    add("schema.verdicts.detail_empty.valid", seal(base_env(verdicts=[{"check": "a", "result": "pass", "detail": ""}])))
    add("schema.verdicts.detail_int", seal(base_env(verdicts=[{"check": "a", "result": "pass", "detail": 5}])))
    add("schema.verdicts.detail_non_ascii", seal(base_env(verdicts=[{"check": "a", "result": "pass", "detail": "é"}])))
    add("schema.verdicts.detail_newline_inside", seal(base_env(verdicts=[{"check": "a", "result": "pass", "detail": "a\nb"}])))
    add("schema.verdicts.extra_key", seal(base_env(verdicts=[{"check": "a", "result": "pass", "x": 1}])))
    add("schema.verdicts.missing_check", seal(base_env(verdicts=[{"result": "pass"}])))
    add("schema.verdicts.check_empty", seal(base_env(verdicts=[{"check": "", "result": "pass"}])))

    # signer
    e = base_env()
    del e["signer"]["did"]
    add("schema.signer.missing_did", seal(e))
    e = base_env()
    e["signer"]["extra"] = 1
    add("schema.signer.extra_key", seal(e))
    add("schema.signer.not_dict", seal(base_env(signer="x")))
    e = base_env()
    e["signer"]["did"] = "did:ORCHA:agent:x"
    add("schema.signer.did_uppercase_method", seal(e))
    e = base_env()
    e["signer"]["did"] = 5
    add("schema.signer.did_int", seal(e))
    e = base_env()
    e["signer"]["did"] = "did:key:z6Mk"
    add("schema.signer.did_other_method.valid", seal(e), "the schema takes any method; the key decides")
    e = base_env()
    e["signer"]["public_key_b64"] = ""
    add("schema.signer.key_empty", seal(e))
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64[:-1]
    add("schema.signer.key_padding_stripped", seal(e), "43 data chars: Python says Incorrect padding -> invalid")
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64 + "="
    add("schema.signer.key_extra_pad.valid", seal(e), "`==` passes the regex; the decoder stops at the first `=` it can use")
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64 + "=="
    add("schema.signer.key_three_pads", seal(e), "`={0,2}` rejects three")
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64 + "\n"
    add("schema.signer.key_trailing_newline.valid", seal(e), "b64decode drops the newline")
    e = base_env()
    e["signer"]["public_key_b64"] = b64_nonstrict_variant(PUB_B64)
    add("schema.signer.key_noncanonical_b64.valid", seal(e),
        "last data char differs in bits Python discards: same key bytes")
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64[:4] + " " + PUB_B64[4:]
    add("schema.signer.key_space_inside", seal(e))
    e = base_env()
    e["signer"]["public_key_b64"] = base64.b64encode(PUB_RAW[:31]).decode()
    add("schema.signer.key_31_bytes", seal(e), "from_public_bytes raises ValueError")
    e = base_env()
    e["signer"]["public_key_b64"] = base64.b64encode(PUB_RAW + b"\x00").decode()
    add("schema.signer.key_33_bytes", seal(e))
    e = base_env()
    e["signer"]["public_key_b64"] = PUB_B64.replace("A", "-", 1) if "A" in PUB_B64 else PUB_B64[:-2] + "-="
    add("schema.signer.key_urlsafe_char", seal(e), "`-` is outside the regex alphabet")

    # signature encoding
    e = valid_env()
    sig_b64 = e["signature"]
    e2 = dict(e, signature=sig_b64.rstrip("="))
    add("schema.signature.padding_stripped", e2, "86 data chars: Incorrect padding")
    e2 = dict(e, signature=sig_b64.rstrip("=") + "=")
    add("schema.signature.one_pad", e2)
    e2 = dict(e, signature=sig_b64 + "=")
    add("schema.signature.three_pads", e2, "regex")
    e2 = dict(e, signature=sig_b64 + "\n")
    add("schema.signature.trailing_newline.valid", e2)
    e2 = dict(e, signature=b64_nonstrict_variant(sig_b64))
    add("schema.signature.noncanonical_b64.valid", e2, "discarded bits differ; same signature bytes")
    e2 = dict(e, signature=sig_b64[:40] + "\n" + sig_b64[40:])
    add("schema.signature.newline_inside", e2, "regex: only one trailing newline is forgiven")
    e2 = dict(e, signature=base64.b64encode(base64.b64decode(sig_b64)[:63]).decode())
    add("schema.signature.63_bytes", e2)
    e2 = dict(e, signature=base64.b64encode(base64.b64decode(sig_b64) + b"\x00").decode())
    add("schema.signature.65_bytes", e2)
    e2 = dict(e, signature="")
    add("schema.signature.empty", e2)
    e2 = dict(e, signature=None)
    add("schema.signature.null", e2)
    e2 = dict(e, signature=sig_b64.replace("/", "_") if "/" in sig_b64 else sig_b64[:-3] + "_==")
    add("schema.signature.urlsafe_char", e2)

    # 6. parse level: whitespace, literals, numbers, strings, structure
    v = valid_env()
    vt = render(v)
    add("parse.indent2.valid", render(v, indent=2))
    add("parse.indent_tabs.valid", render(v, indent="\t"))
    add("parse.leading_ws.valid", " \t\r\n" + vt)
    add("parse.trailing_ws.valid", vt + " \t\r\n")
    add("parse.keys_reversed.valid", render(dict(reversed(list(v.items())))), "canonical form sorts keys")
    add("parse.key_with_escape.valid", vt.replace('"format"', '"\\u0066ormat"', 1), "escaped key spells `format`")
    add("parse.trailing_garbage", vt + "x")
    add("parse.trailing_second_object", vt + "{}")
    add("parse.trailing_null", vt + " null")
    add("parse.leading_bom", "﻿" + vt, "json.loads(str) refuses a BOM")
    add("parse.bom_after_ws", " ﻿" + vt)
    add("parse.empty", b"")
    add("parse.ws_only", b" \n\t")
    add("parse.top_null", b"null", "not an object")
    add("parse.top_array", b"[]")
    add("parse.top_string", b'"x"')
    add("parse.top_number", b"1")
    add("parse.top_true", b"true")
    add("parse.empty_object", b"{}", "an object: invalid, run_id/signer/steps all null")
    add("parse.trailing_comma_obj", b'{"a":1,}')
    add("parse.trailing_comma_arr", b'{"a":[1,]}')
    add("parse.single_quotes", b"{'a':1}")
    add("parse.unquoted_key", b"{a:1}")
    add("parse.non_string_key", b"{1:2}")
    add("parse.missing_colon", b'{"a" 1}')
    add("parse.missing_comma_obj", b'{"a":1 "b":2}')
    add("parse.missing_comma_arr", b'{"a":[1 2]}')
    add("parse.unterminated_string", b'{"a":"x')
    add("parse.unterminated_object", b'{"a":1')
    add("parse.unterminated_array", b'{"a":[1')
    add("parse.nul_byte_outside", b'{"a":1}\x00')
    add("parse.num_leading_zero", b'{"a":01}')
    add("parse.num_trailing_dot", b'{"a":1.}')
    add("parse.num_leading_dot", b'{"a":.5}')
    add("parse.num_plus", b'{"a":+1}')
    add("parse.num_minus_only", b'{"a":-}')
    add("parse.num_exp_no_digits", b'{"a":1e}')
    add("parse.num_exp_sign_no_digits", b'{"a":1e+}')
    add("parse.num_hex", b'{"a":0x1}')
    add("parse.num_minus_space", b'{"a":- 1}')
    add("parse.num_1e5.invalid", b'{"a":1e5}', "parses as float; schema fails; problem is null")
    add("parse.num_neg_zero_top.invalid", b'{"a":-0}')
    add("parse.num_big_top.invalid", ('{"a":' + "9" * 400 + "}").encode())
    add("parse.lit_True", b'{"a":True}')
    add("parse.lit_nul", b'{"a":nul}')
    add("parse.lit_nan_lower", b'{"a":nan}')
    add("parse.lit_minus_NaN", b'{"a":-NaN}', "Python only takes `-Infinity`")
    add("parse.lit_infinity_lower", b'{"a":infinity}')
    add("parse.lit_NaN.invalid", b'{"a":NaN}')
    add("parse.lit_Infinity.invalid", b'{"a":Infinity}')
    add("parse.lit_neg_Infinity.invalid", b'{"a":-Infinity}')
    add("parse.lit_Infinity_prefix", b'{"a":Infinityx}')
    add("parse.lit_true_prefix", b'{"a":truex}')
    add("parse.str_raw_control", b'{"a":"\x01"}', "strict json: Invalid control character")
    add("parse.str_raw_tab", b'{"a":"\t"}', "a raw tab is a control character too")
    add("parse.str_raw_newline", b'{"a":"\n"}')
    add("parse.str_raw_del.invalid", b'{"a":"\x7f"}', "0x7f is allowed by the JSON grammar")
    add("parse.str_escaped_control.invalid", b'{"a":"\\u0001"}')
    add("parse.str_bad_escape", b'{"a":"\\x"}')
    add("parse.str_short_u_escape", b'{"a":"\\u12"}')
    add("parse.str_u_escape_nonhex", b'{"a":"\\u12G4"}')
    add("parse.str_high_surrogate_then_bad_u", b'{"a":"\\ud83d\\uZZZZ"}', "the second escape is parsed and rejected")
    add("parse.str_high_surrogate_then_other_escape.invalid", b'{"a":"\\ud83d\\n"}', "lone high surrogate kept")
    add("parse.str_lone_low_surrogate.invalid", b'{"a":"\\udc00"}')
    add("parse.str_pair.invalid", b'{"a":"\\uD83D\\uDE00"}')
    add("parse.str_raw_non_ascii.invalid", '{"a":"é"}'.encode())
    add("parse.str_raw_astral.invalid", '{"a":"\U0001f600"}'.encode())
    add("parse.str_slash_escape.invalid", b'{"a":"\\/"}')
    add("parse.backslash_outside", b'{"a":\\1}')
    add("parse.comment", b'{"a":1 /* c */}')
    add("parse.utf16le_bom", b"\xff\xfe{\x00}\x00", "UTF-16 bytes are invalid UTF-8")
    add("parse.utf8_invalid_start_byte", b'{"a":"\xff"}', "UnicodeDecodeError: the CLI tracebacks")
    add("parse.utf8_overlong", b'{"a":"\xc0\x80"}')
    add("parse.utf8_encoded_surrogate", b'{"a":"\xed\xa0\x80"}')
    add("parse.utf8_truncated_tail", b'{"a":"\xe2\x82"}')
    add("parse.utf8_above_max", b'{"a":"\xf4\x90\x80\x80"}')
    add("parse.utf8_continuation_alone", b'{"a":"\x80"}')

    # duplicate keys: last one wins
    first_wrong = vt.replace("{", '{"format": "wrong", ', 1)
    add("parse.dup_key_first_wrong_last_right.valid", first_wrong, "last duplicate wins")
    last_wrong = vt[: vt.rfind("}")] + ', "format": "wrong"}'
    add("parse.dup_key_last_wrong", last_wrong, "last duplicate wins -> schema fails")
    sig_first = vt.replace("{", '{"signature": "AAAA", ', 1)
    add("parse.dup_signature_first_bad_last_good.valid", sig_first)
    sig_last = vt[: vt.rfind("}")] + ', "signature": "AAAA"}'
    add("parse.dup_signature_last_bad", sig_last, "dies at signature")
    add("parse.dup_key_in_step.valid", vt.replace('"seq": 0', '"seq": 5, "seq": 0', 1), "last wins inside a step")

    # nesting (an extra key holds the nested arrays: the schema fails on the key set)
    for depth in (64, 65, 300, 2000):
        add(f"parse.nesting_{depth}.invalid", ('{"x":' + "[" * depth + "]" * depth + "}").encode(),
            "parses in CPython (C recursion limit ~10000); schema fails")
    add("parse.nesting_20000", ('{"x":' + "[" * 20000 + "]" * 20000 + "}").encode(),
        "RecursionError in json.loads: the CLI tracebacks with no verdict")
    add("parse.nesting_20000_objects", ('{"x":' + '{"a":' * 20000 + "1" + "}" * 20000 + "}").encode())

    # 7. Ed25519 edge vectors (verdicts from the sdk; built with the reference arithmetic)
    e = valid_env()
    sig = base64.b64decode(e["signature"])
    r_bytes, s_int = sig[:32], int.from_bytes(sig[32:], "little")
    add("ed25519.s_plus_L", with_sig(e, r_bytes + (s_int + L).to_bytes(32, "little")),
        "same group element, S >= L: must be rejected")
    add("ed25519.s_eq_L", with_sig(e, r_bytes + L.to_bytes(32, "little")))
    add("ed25519.s_all_ff", with_sig(e, r_bytes + b"\xff" * 32))
    add("ed25519.s_zero_real_key", with_sig(e, r_bytes + bytes(32)))
    add("ed25519.r_all_zero", with_sig(e, bytes(32) + sig[32:]), "R = the order-4 point (0, sqrt(-1))... y=0 encodes a valid point")
    add("ed25519.r_not_on_curve", with_sig(e, bytes.fromhex("02" + "00" * 31) + sig[32:]), "y=2 has no square root")
    add("ed25519.r_noncanonical_y", with_sig(e, bytes.fromhex("ee" + "ff" * 30 + "7f") + sig[32:]), "y = p+1")
    add("ed25519.sig_all_zero", with_sig(e, bytes(64)))

    msg_for = digest_msg  # the signed message is the digest of the envelope as it will be read

    # small-order public keys, each with a crafted (R, S) that satisfies the group equation
    for i, hx in enumerate(TORSION_HEX):
        a_bytes = bytes.fromhex(hx)
        a_point = pt_decode(a_bytes)
        assert a_point is not None
        env_u = base_env()
        env_u["signer"]["public_key_b64"] = base64.b64encode(a_bytes).decode()
        env_u = seal(env_u)
        crafted = craft_small_order_sig(a_bytes, a_point, msg_for(env_u))
        add(f"ed25519.small_order_key_{i}_order{pt_order_small(a_point)}", with_sig(env_u, crafted),
            "small-order public key with a signature that satisfies S*B = R + k*A")

    # the identity key with the sign bit set: x=0 and x_0=1, which RFC 8032 rejects on decode
    a_bytes = bytes.fromhex("01" + "00" * 30 + "80")
    env_u = base_env()
    env_u["signer"]["public_key_b64"] = base64.b64encode(a_bytes).decode()
    env_u = seal(env_u)
    crafted = craft_small_order_sig(a_bytes, (0, 1, 1, 0), msg_for(env_u))
    add("ed25519.identity_key_sign_bit", with_sig(env_u, crafted), "x=0 with sign bit 1")

    # non-canonical key encodings: y >= p, which reduce to the identity (y=p+1) and the order-4 point (y=p)
    for label, y_val, sign_bit in (("identity_y_p_plus_1", P + 1, 0), ("identity_y_p_plus_1_sign", P + 1, 1),
                                   ("order4_y_p", P, 0), ("order4_y_p_sign", P, 1)):
        a_bytes = bytearray(y_val.to_bytes(32, "little"))
        a_bytes[31] |= sign_bit << 7
        a_bytes = bytes(a_bytes)
        reduced = bytearray((y_val % P).to_bytes(32, "little"))
        reduced[31] |= sign_bit << 7
        a_point = pt_decode(bytes(reduced))
        if a_point is None:  # x=0 with sign 1: the point (0, 1) or (0, -1) regardless of the bit
            a_point = pt_decode(bytes(reduced[:31]) + bytes([reduced[31] & 0x7F]))
        assert a_point is not None
        env_u = base_env()
        env_u["signer"]["public_key_b64"] = base64.b64encode(a_bytes).decode()
        env_u = seal(env_u)
        crafted = craft_small_order_sig(a_bytes, a_point, msg_for(env_u))
        add(f"ed25519.noncanonical_key_{label}", with_sig(env_u, crafted),
            "key encoding with y >= p; crafted signature for the reduced point")

    # identity key, S = 0 and R = enc(O): the smallest possible forgery shape
    a_bytes = bytes.fromhex(TORSION_HEX[0])
    env_u = base_env()
    env_u["signer"]["public_key_b64"] = base64.b64encode(a_bytes).decode()
    env_u = seal(env_u)
    add("ed25519.identity_key_s0_r_identity", with_sig(env_u, a_bytes + bytes(32)), "S=0, R=O")
    add("ed25519.identity_key_s0_r_noncanonical", with_sig(env_u, bytes.fromhex("ee" + "ff" * 30 + "7f") + bytes(32)),
        "R encodes y=p+1 (the identity, non-canonically)")
    add("ed25519.identity_key_s0_r_sign_bit", with_sig(env_u, bytes.fromhex("01" + "00" * 30 + "80") + bytes(32)),
        "R = identity with the sign bit set")

    # a real key with a torsion component: A' = A + T8. Cofactorless verification
    # (what OpenSSL does) needs k*T = O, i.e. k mod 8 == 0; cofactored would accept both.
    a_scalar, _prefix = scalar_of(KEY)
    A = pt_mul(BASE, a_scalar)
    assert pt_encode(A) == PUB_RAW
    T8 = pt_decode(bytes.fromhex(TORSION_HEX[1]))
    A_dirty = pt_add(A, T8)
    A_dirty_bytes = pt_encode(A_dirty)
    env_u = base_env()
    env_u["signer"]["public_key_b64"] = base64.b64encode(A_dirty_bytes).decode()
    env_u = seal(env_u)
    msg = msg_for(env_u)
    found = {}
    r_seed = 1
    while len(found) < 2:
        r_val = int.from_bytes(sha512(b"parity-r", r_seed.to_bytes(4, "big")), "little") % L
        R = pt_encode(pt_mul(BASE, r_val))
        k = int.from_bytes(sha512(R, A_dirty_bytes, msg), "little") % L
        S = (r_val + k * a_scalar) % L
        bucket = "k_mod8_zero" if k % 8 == 0 else "k_mod8_nonzero"
        found.setdefault(bucket, R + S.to_bytes(32, "little"))
        r_seed += 1
    add("ed25519.torsion_key_k_mod8_zero", with_sig(env_u, found["k_mod8_zero"]),
        "A' = A + T8, honest-style signature, k*T8 = O: both check styles agree")
    add("ed25519.torsion_key_k_mod8_nonzero", with_sig(env_u, found["k_mod8_nonzero"]),
        "A' = A + T8 with k*T8 != O: a cofactored check passes, a cofactorless one fails")

    # a 32-byte key that is not a curve point, and the all-0xff key
    env_u = base_env()
    env_u["signer"]["public_key_b64"] = base64.b64encode(bytes.fromhex("02" + "00" * 31)).decode()
    add("ed25519.key_not_on_curve", seal(env_u), "y=2 has no square root")
    env_u = base_env()
    env_u["signer"]["public_key_b64"] = base64.b64encode(b"\xff" * 32).decode()
    add("ed25519.key_all_ff", seal(env_u), "y = 2^255-1 (>= p) with the sign bit")


# ──────────────────────────────── probing ─────────────────────────────────────


def probe() -> None:
    """Print the sdk's verdict for the Ed25519 edge cases (to learn the OpenSSL rules)."""
    for c in CASES:
        if not c["name"].startswith("ed25519."):
            continue
        exp = expect_for(c["bytes"])["expect"]
        print(f"{c['name']:<55} valid={exp['valid']!s:<5} checks={''.join('T' if exp['checks'][k] else 'F' for k in CHECK_KEYS)}  {c['note']}")


# ───────────────────────────────── main ───────────────────────────────────────


def nd_ranges() -> list[list[int]]:
    """Python's `\\d` (Unicode Nd) as inclusive code point ranges."""
    d = re.compile(r"\d")
    ranges: list[list[int]] = []
    for cp in range(0x110000):
        if d.match(chr(cp)):
            if ranges and ranges[-1][1] == cp - 1:
                ranges[-1][1] = cp
            else:
                ranges.append([cp, cp])
    return ranges


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-cli", action="store_true", help="skip the `orcha verify --json` cross-check")
    ap.add_argument("--probe", action="store_true", help="print the sdk's verdicts for the ed25519 edge cases and exit")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    golden, golden_source = load_golden()
    build_cases(golden)
    if args.probe:
        probe()
        return 0

    disagreements: list[str] = []
    cli_checked = not args.no_cli and ORCHA_BIN.is_file()
    openssl = None
    try:
        from cryptography.hazmat.backends.openssl import backend as _b  # type: ignore

        openssl = _b.openssl_version_text()
    except Exception:  # pragma: no cover - informational only
        pass
    import cryptography

    out_cases = []
    with tempfile.TemporaryDirectory(prefix="mbverify-parity-") as tmp:
        work = Path(tmp)
        (work / "mh").mkdir()
        for c in CASES:
            res = expect_for(c["bytes"])
            row: dict[str, Any] = {"name": c["name"], "bytes_b64": base64.b64encode(c["bytes"]).decode("ascii"),
                                   "expect": res["expect"]}
            if c["note"]:
                row["note"] = c["note"]
            if "sdk_exception" in res:
                row["sdk_exception"] = res["sdk_exception"]
            if cli_checked:
                cli = cli_for(c["bytes"], work)
                row["cli"] = {"exit": cli["exit"], "json": cli["json"] is not None}
                if cli["json"] is None and cli["stderr"]:
                    row["cli"]["stderr"] = cli["stderr"]
                if not cli_agrees(res["expect"], cli):
                    disagreements.append(
                        f"{c['name']}: oracle={res['expect']['verdict']} problem={res['expect']['problem']} "
                        f"checks={res['expect']['checks']} | cli exit={cli['exit']} json={cli['json']} stderr={cli['stderr']}")
            out_cases.append(row)

    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "tests/parity.py",
        "sdk_version": emerge.__version__,
        "sdk_src": "sdk/src",
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "int_max_str_digits": sys.get_int_max_str_digits(),
        "cryptography": cryptography.__version__,
        "openssl": openssl,
        "golden_source": golden_source,
        "cli_checked": cli_checked,
        "orcha_bin": "orcha" if cli_checked else None,
        "nd_ranges": nd_ranges(),
        "case_count": len(out_cases),
        "valid_count": sum(1 for r in out_cases if r["expect"]["valid"]),
        "malformed_count": sum(1 for r in out_cases if r["expect"]["problem"] == "malformed"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"meta": meta, "cases": out_cases}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out} : {meta['case_count']} cases, {meta['valid_count']} valid, "
          f"{meta['malformed_count']} malformed, cli_checked={cli_checked}")
    if disagreements:
        print(f"\n{len(disagreements)} CLI disagreement(s) with the in-process oracle:")
        for line in disagreements:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
