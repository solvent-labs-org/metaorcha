from __future__ import annotations

from superagent.nodes.execute_agent_calls import _canvas_manifest_id


def test_generates_a_per_call_id_when_the_manifest_supplies_none():
    manifest = {"version": "1.0", "layout": "single", "components": []}
    assert _canvas_manifest_id(manifest, "call_1") == "canvas-call_1"


def test_uses_a_supplied_manifest_id_verbatim():
    manifest = {"version": "1.0", "id": "finance-dashboard", "layout": "single"}
    assert _canvas_manifest_id(manifest, "call_1") == "finance-dashboard"


def test_the_supplied_id_is_stable_across_calls():
    # The whole point: the same dashboard re-rendered on a later run addresses
    # the same canvas, so the client can update in place instead of appending.
    manifest = {"version": "1.0", "id": "finance-dashboard", "layout": "single"}
    first = _canvas_manifest_id(manifest, "call_1")
    second = _canvas_manifest_id(manifest, "call_99")
    assert first == second == "finance-dashboard"


def test_generated_ids_still_differ_per_call_without_a_supplied_id():
    manifest = {"version": "1.0", "layout": "single"}
    assert _canvas_manifest_id(manifest, "call_1") != _canvas_manifest_id(
        manifest, "call_2"
    )


def test_surrounding_whitespace_is_trimmed():
    manifest = {"version": "1.0", "id": "  finance-dashboard  "}
    assert _canvas_manifest_id(manifest, "call_1") == "finance-dashboard"


def test_a_blank_id_falls_back_rather_than_emitting_an_empty_identifier():
    # An empty manifest_id would collide with every other blank one on the
    # client, which is worse than a generated id.
    for blank in ("", "   ", "\t\n"):
        manifest = {"version": "1.0", "id": blank}
        assert _canvas_manifest_id(manifest, "call_1") == "canvas-call_1"


def test_a_non_string_id_falls_back():
    # The envelope is agent-supplied and unvalidated at this point, so a number
    # or a nested object must not reach the SSE event as a manifest_id.
    for bad in (42, 1.5, True, None, ["a"], {"nested": "object"}):
        manifest = {"version": "1.0", "id": bad}
        assert _canvas_manifest_id(manifest, "call_1") == "canvas-call_1"


def test_a_non_dict_manifest_falls_back():
    for bad in (None, "not-a-manifest", 7, ["components"]):
        assert _canvas_manifest_id(bad, "call_1") == "canvas-call_1"
