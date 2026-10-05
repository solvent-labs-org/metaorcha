"""AD-16: the value a step's ``output_hash`` commits to.

``output_preimage`` must be total (any handler result, never raises),
deterministic (the same result always gives the same bytes) and JSON-only (the
canonical form cannot render anything else). ``redact_credentials`` must
replace the exact bytes of each credential with a fixed marker, so anyone
holding the redacted output re-derives the hash.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import hashlib
import json

from emerge.preimage import (
    CYCLE_MARKER,
    DEPTH_MARKER,
    MAX_DEPTH,
    output_preimage,
    redact_credentials,
    redaction_marker,
)
from emerge.run_attestation import canonical_json_bytes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_only(value) -> None:
    json.dumps(value, allow_nan=False)


# -- stand-ins for MCP content blocks (the SDK never imports an MCP library) --


class TextContent:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class ImageContent:
    type = "image"

    def __init__(self, data: str, mime: str = "image/png") -> None:
        self.data = data
        self.mimeType = mime


class BlobResource:
    def __init__(self, blob: str) -> None:
        self.blob = blob


class EmbeddedResource:
    type = "resource"

    def __init__(self, resource) -> None:
        self.resource = resource


class Dumpable:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict:
        assert mode == "json"
        return self._payload


class Opaque:
    """Default repr carries a memory address — never a stable rendering."""


class Level(enum.IntEnum):
    HIGH = 3


@dataclasses.dataclass
class Row:
    name: str
    count: int


def test_scalars_pass_through_as_their_builtin_type() -> None:
    assert output_preimage("text") == "text"
    assert output_preimage(7) == 7
    assert output_preimage(True) is True
    assert output_preimage(None) is None
    assert output_preimage(1.5) == 1.5
    level = output_preimage(Level.HIGH)
    assert level == 3 and type(level) is int


def test_non_finite_floats_become_strings() -> None:
    assert output_preimage(float("nan")) == "nan"
    assert output_preimage(float("inf")) == "inf"
    _json_only(output_preimage([float("-inf")]))


def test_binary_maps_to_its_sha256_hex() -> None:
    assert output_preimage(b"\x00\x01") == _sha(b"\x00\x01")
    assert output_preimage(bytearray(b"ab")) == _sha(b"ab")


def test_content_blocks_map_to_a_list_of_strings() -> None:
    png = b"\x89PNG-bytes"
    blocks = [
        TextContent("first"),
        ImageContent(base64.b64encode(png).decode()),
        EmbeddedResource(BlobResource(base64.b64encode(b"doc").decode())),
        EmbeddedResource(TextContent("inner")),
    ]
    assert output_preimage(blocks) == ["first", _sha(png), _sha(b"doc"), "inner"]


def test_non_base64_binary_payload_hashes_its_text() -> None:
    assert output_preimage([ImageContent("not base64!")]) == [_sha(b"not base64!")]


def test_json_objects_recurse_and_keys_become_strings() -> None:
    value = {"a": [1, {"b": b"x"}], 2: "two", None: "none"}
    assert output_preimage(value) == {
        "a": [1, {"b": _sha(b"x")}],
        "2": "two",
        "None": "none",
    }


def test_dict_blocks_stay_json_rather_than_being_flattened() -> None:
    raw = {"content": [{"type": "text", "text": "hi"}], "isError": False}
    assert output_preimage(raw) == raw


def test_models_and_dataclasses_render_through_their_data() -> None:
    assert output_preimage(Dumpable({"k": b"v"})) == {"k": _sha(b"v")}
    assert output_preimage(Row("r", 2)) == {"name": "r", "count": 2}


def test_opaque_objects_render_deterministically() -> None:
    first, second = output_preimage(Opaque()), output_preimage(Opaque())
    assert first == second == "Opaque"


def test_sets_are_sorted() -> None:
    assert output_preimage({"b", "a", "c"}) == ["a", "b", "c"]
    assert output_preimage(frozenset({3, 1})) == [1, 3]


def test_cycles_and_depth_are_cut_with_markers() -> None:
    loop: list = []
    loop.append(loop)
    assert output_preimage(loop) == [CYCLE_MARKER]

    deep: list = []
    node = deep
    for _ in range(MAX_DEPTH + 10):
        child: list = []
        node.append(child)
        node = child
    rendered = output_preimage(deep)
    _json_only(rendered)
    assert DEPTH_MARKER in json.dumps(rendered)


def test_shared_subobjects_are_not_mistaken_for_cycles() -> None:
    shared = {"x": 1}
    assert output_preimage([shared, shared]) == [{"x": 1}, {"x": 1}]


def test_never_raises_on_hostile_objects() -> None:
    class Hostile:
        @property
        def text(self):
            raise RuntimeError("boom")

        def model_dump(self, mode: str = "python"):
            raise RuntimeError("boom")

        def __str__(self) -> str:
            raise RuntimeError("boom")

    name = Hostile.__qualname__
    assert output_preimage(Hostile()) == name
    assert output_preimage([Hostile(), "ok"]) == [name, "ok"]


def test_same_result_same_bytes() -> None:
    def make():
        return {"z": [TextContent("t"), b"\x02"], "a": {"n": 1}}

    assert canonical_json_bytes(output_preimage(make())) == canonical_json_bytes(
        output_preimage(make())
    )


def test_output_is_json_only() -> None:
    _json_only(
        output_preimage(
            {"f": float("nan"), "b": b"x", "o": Opaque(), "s": {1}, "e": Level.HIGH}
        )
    )


# -- redaction --------------------------------------------------------------


def test_redaction_replaces_exact_bytes_with_the_marker() -> None:
    out = redact_credentials(
        "token=s3cr3t-value; again s3cr3t-value", {"API_KEY": "s3cr3t-value"}
    )
    assert out == "token=[REDACTED:API_KEY]; again [REDACTED:API_KEY]"
    assert redaction_marker("API_KEY") == "[REDACTED:API_KEY]"


def test_redaction_walks_lists_objects_and_keys() -> None:
    value = {"s3cr3t": ["x s3cr3t", 5, None], "n": "clean"}
    assert redact_credentials(value, [("K", "s3cr3t")]) == {
        "[REDACTED:K]": ["x [REDACTED:K]", 5, None],
        "n": "clean",
    }


def test_longer_secret_wins_over_a_contained_shorter_one() -> None:
    creds = {"SHORT": "abc", "LONG": "abc123"}
    assert redact_credentials("abc123 abc", creds) == "[REDACTED:LONG] [REDACTED:SHORT]"


def test_marker_text_is_never_rewritten() -> None:
    # A secret that occurs inside the marker text is replaced once, in the
    # original bytes only.
    assert redact_credentials("RED", {"X": "RED"}) == "[REDACTED:X]"


def test_same_secret_under_two_names_uses_the_smallest_name() -> None:
    creds = [("ZED", "tok"), ("ALPHA", "tok")]
    assert redact_credentials("tok", creds) == "[REDACTED:ALPHA]"


def test_empty_and_non_string_secrets_are_ignored() -> None:
    assert redact_credentials("abc", {"E": "", "N": None}) == "abc"  # type: ignore[dict-item]
    assert redact_credentials("abc", {}) == "abc"


def test_redaction_is_deterministic_for_the_hash() -> None:
    creds = {"B": "bbb", "A": "aaa"}
    once = canonical_json_bytes(redact_credentials(["aaa bbb"], creds))
    again = canonical_json_bytes(
        redact_credentials(["aaa bbb"], dict(reversed(list(creds.items()))))
    )
    assert once == again
