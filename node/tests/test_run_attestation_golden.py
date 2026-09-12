"""RFC 0003 golden vectors verified with emerge_node primitives only.

This is a deliberately independent re-implementation of the RFC verification
algorithm (hashlib + canonical_json_bytes + verify_bytes) — no validator or
SDK code — so the shared fixture in
docs/spec/test-vectors/run-attestation-golden.json proves byte-exact
conformance across implementations.

It also pins the SPECIFICATION to the implementation. The RFC's normative
values are parsed out of the document itself rather than transcribed into
constants here, so a change to the RFC text that the code does not follow —
or a change to the code the RFC does not describe — fails a test instead of
sitting green. Nothing else in this repository compares the two.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from emerge_node.envelope import canonical_json_bytes, verify_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = (
    REPO_ROOT / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"
)
RFC_PATH = REPO_ROOT / "docs" / "spec" / "rfcs" / "0003-run-attestation-envelope.md"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _steps_root(steps: list[dict]) -> str:
    """RFC 'steps_root — linear chain' rule, reimplemented from the prose.

    Links on the 32 RAW digest bytes, never their hexadecimal rendering.
    """
    if not steps:
        return _sha256_hex(b"")
    previous = b""
    for index, step in enumerate(steps):
        s_i = canonical_json_bytes(step)
        material = s_i if index == 0 else previous + s_i
        previous = hashlib.sha256(material).digest()
    return previous.hex()


def _steps_merkle_root(steps: list[dict]) -> str:
    """RFC 'steps_merkle_root — Merkle Tree Hash' rule (RFC 6962 §2.1)."""

    def mth(data: list[bytes]) -> bytes:
        if not data:
            return hashlib.sha256(b"").digest()
        if len(data) == 1:
            return hashlib.sha256(b"\x00" + data[0]).digest()
        split = 1
        while split * 2 < len(data):
            split *= 2
        return hashlib.sha256(b"\x01" + mth(data[:split]) + mth(data[split:])).digest()

    return mth([canonical_json_bytes(step) for step in steps]).hex()


def _verify(envelope: dict) -> bool:
    """RFC verification steps 2–5 (the fixture is known schema-valid)."""
    if _steps_root(envelope["steps"]) != envelope["steps_root"]:
        return False
    if _steps_merkle_root(envelope["steps"]) != envelope["steps_merkle_root"]:
        return False
    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
    digest = _sha256_hex(canonical_json_bytes(unsigned))
    return verify_bytes(
        digest.encode("utf-8"),
        envelope["signature"],
        envelope["signer"]["public_key_b64"],
    )


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rfc() -> str:
    return RFC_PATH.read_text(encoding="utf-8")


def _rfc_value(rfc_text: str, pattern: str) -> str:
    """Pull one normative 64-hex value out of the RFC prose."""
    match = re.search(pattern, rfc_text, re.S)
    assert match, f"RFC 0003 no longer states a value matching {pattern!r}"
    return match.group(1)


def _rfc_worked_example(rfc_text: str) -> dict:
    blocks = re.findall(r"```json\n(.*?)\n```", rfc_text, re.S)
    assert len(blocks) == 2, "expected the schema block and the worked example"
    return json.loads(blocks[1])


# --- spec/implementation pin -------------------------------------------------


def test_rfc_worked_example_is_the_golden_fixture(golden, rfc):
    """The RFC's example and the shared fixture cannot drift apart silently."""
    assert _rfc_worked_example(rfc) == golden["valid"]


def test_rfc_prose_values_match_recomputation(golden, rfc):
    """Every 64-hex value the RFC states in prose is reproduced from its rules."""
    valid = golden["valid"]
    assert _sha256_hex(canonical_json_bytes(valid["steps"][0])) == _rfc_value(
        rfc, r"chain head `h_0 =\s*([0-9a-f]{64})`"
    )
    unsigned = {k: v for k, v in valid.items() if k != "signature"}
    assert _sha256_hex(canonical_json_bytes(unsigned)) == _rfc_value(
        rfc, r"signed digest is\s*`([0-9a-f]{64})`"
    )


def test_rfc_empty_run_constant_matches_both_constructions(rfc):
    empty = _rfc_value(rfc, r"\*\*Empty run\*\* \(`n = 0`\).*?`([0-9a-f]{64})`")
    assert _steps_root([]) == empty
    assert _steps_merkle_root([]) == empty


# --- byte-exact conformance --------------------------------------------------


def test_golden_roots_recompute(golden):
    valid = golden["valid"]
    assert _steps_root(valid["steps"]) == valid["steps_root"]
    assert _steps_merkle_root(valid["steps"]) == valid["steps_merkle_root"]


def test_golden_two_roots_are_distinct(golden):
    """Chain and tree are independent commitments; equality would be a bug."""
    valid = golden["valid"]
    assert valid["steps_root"] != valid["steps_merkle_root"]


def test_chain_links_on_raw_bytes_not_ascii_hex(golden):
    """The construction that was rejected must not reproduce the fixture."""
    steps = golden["valid"]["steps"]
    previous = ""
    for index, step in enumerate(steps):
        s_i = canonical_json_bytes(step)
        material = s_i if index == 0 else previous.encode("ascii") + s_i
        previous = _sha256_hex(material)
    assert previous != golden["valid"]["steps_root"]


def test_merkle_split_rule_rejects_duplicate_padding():
    """RFC 6962 promotes a lone right-hand node; padding by duplication collides."""

    def duplicating_root(leaves: list[bytes]) -> bytes:
        level = [hashlib.sha256(leaf).digest() for leaf in leaves]
        while len(level) > 1:
            if len(level) % 2:
                level.append(level[-1])
            level = [
                hashlib.sha256(level[i] + level[i + 1]).digest()
                for i in range(0, len(level), 2)
            ]
        return level[0]

    three = [{"seq": i} for i in range(3)]
    padded = three + [three[-1]]
    # The rejected convention cannot tell these two step lists apart...
    assert duplicating_root([canonical_json_bytes(s) for s in three]) == (
        duplicating_root([canonical_json_bytes(s) for s in padded])
    )
    # ...and the specified one can (CVE-2012-2459).
    assert _steps_merkle_root(three) != _steps_merkle_root(padded)


def test_golden_valid_envelope_verifies(golden):
    assert _verify(golden["valid"]) is True


# --- RFC 8785 parity ("Canonical form and RFC 8785") -------------------------


def _jcs(value) -> bytes:
    r"""RFC 8785 (JCS) serialization, implemented from the RFC's own rules.

    Independent of ``canonical_json_bytes`` on purpose: this is the *other*
    canonical form the RFC claims parity with. Section 3.2.2.2 - strings
    escape ``"`` and ``\``, use the short forms for ``\b \t \n \f \r``,
    ``\u00XX`` (lowercase) for the remaining C0 controls, and pass every
    other character through as raw UTF-8 (DEL included). Section 3.2.3 -
    object keys sort by UTF-16 code units. Section 3.2.2.3 - numbers render
    per ECMAScript ``Number::toString``; for the integers this envelope
    carries that is plain decimal digits, and floats are refused here because
    their JCS rendering is exactly what the RFC says a JCS verifier must NOT
    rely on.
    """
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        assert abs(value) < 2**53
        return str(value).encode("ascii")
    if isinstance(value, float):
        raise TypeError("JCS float rendering is outside the parity claim")
    if isinstance(value, str):
        short = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
        out = ['"']
        for ch in value:
            if ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif ch in short:
                out.append(short[ch])
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(_jcs(v) for v in value) + b"]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return b"{" + b",".join(_jcs(k) + b":" + _jcs(v) for k, v in items) + b"}"
    raise TypeError(type(value))


def test_jcs_and_reference_agree_on_the_constrained_envelope(golden):
    """Core verification is byte-identical under RFC 8785 and under Python.

    Asserted where the RFC makes the claim: each canonical step ``s_i``, the
    unsigned envelope (the digest input), and therefore both roots and the
    digest.
    """
    for vector in ("valid", "tampered"):
        envelope = golden[vector]
        for step in envelope["steps"]:
            assert _jcs(step) == canonical_json_bytes(step)
        unsigned = {k: v for k, v in envelope.items() if k != "signature"}
        assert _jcs(unsigned) == canonical_json_bytes(unsigned)
        assert _jcs(envelope) == canonical_json_bytes(envelope)
    valid = golden["valid"]
    assert _steps_root(valid["steps"]) == valid["steps_root"]


def test_parity_claim_stops_at_the_charset_rule():
    """Negative control: the constraint is what makes the claim true.

    One character outside printable ASCII, and the two forms diverge — which
    is why the RFC says a JCS verifier must render disclosed raw
    ``args``/``output`` with the reference form, not with JCS.
    """
    conformant = {"tool": "search_docs", "seq": 0}
    assert _jcs(conformant) == canonical_json_bytes(conformant)
    non_ascii = {"tool": "r\u00e9sum\u00e9", "seq": 0}
    assert _jcs(non_ascii) != canonical_json_bytes(non_ascii)
    del_char = {"tool": "x\x7f", "seq": 0}
    assert _jcs(del_char) != canonical_json_bytes(del_char)
    with pytest.raises(TypeError):
        _jcs({"cdv_score": 0.91})


def test_rfc_states_no_number_rendering_rule_and_no_float_field(rfc):
    """The float field and its cross-language rendering rule are gone."""
    assert "cdv_score" not in rfc
    assert "Number serialization" not in rfc
    assert "cdv_bp" in rfc


def test_golden_tampered_envelope_fails(golden):
    assert _verify(golden["tampered"]) is False
