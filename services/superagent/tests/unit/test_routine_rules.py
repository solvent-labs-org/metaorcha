"""Story 2.1: what a routine may hold, decided before the Gateway persists it.

``check_routine`` and ``POST /routines/validate``: connections must be current
connections, every allow names one of them and a capability it exposes, and
no allow may resolve to ``destructive`` — including a capability the class
rules do not recognise (AD-18: unknown resolves to destructive). Criteria use
the chat path's vocabulary; operands are short printable ASCII (AD-19).
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from superagent.workflow.routine_rules import (
    MAX_CRITERIA,
    RoutineRejected,
    check_criteria,
    check_routine,
)

DID = "did:orcha:agent:docs-mcp-0a1b2c3d"
OTHER = "did:orcha:agent:other-99887766"
NOTION = "did:orcha:agent:notion-5e6f7a8b"
COUNTS = {
    "left": f"{DID}#list_issues",
    "left_path": "/total_count",
    "right": f"{NOTION}#query_database",
    "right_path": "/results",
    "key": "open_issues",
}


def _manifest(**over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "agent_id": DID,
        "tags": ["mcp", "user", "connection"],
        "is_active": True,
        "capabilities": [
            {"capability_id": c}
            for c in (
                "list_issues",
                "create_comment",
                "delete_repo",
                "frobnicate",
                "update_label",
            )
        ],
    }
    m.update(over)
    return m


def _reader(manifests: dict[str, dict[str, Any]]):
    async def read(did: str) -> dict[str, Any]:
        if did not in manifests:
            raise RuntimeError("registry unreachable")
        return manifests[did]

    return read


async def _check(allow: list[str], *, manifest: dict | None = None, **kw: Any):
    return await check_routine(
        connections=kw.pop("connections", [DID]),
        scope_allow=allow,
        criteria=kw.pop("criteria", {}),
        criteria_operands=kw.pop("criteria_operands", {}),
        read_manifest=kw.pop("read_manifest", _reader({DID: manifest or _manifest()})),
    )


@pytest.mark.asyncio
async def test_read_and_write_allows_are_accepted_with_their_class() -> None:
    classes = await _check([f"{DID}#list_issues", f"{DID}#create_comment"])
    assert classes == {f"{DID}#list_issues": "read", f"{DID}#create_comment": "write"}


@pytest.mark.asyncio
async def test_no_allows_is_a_read_only_routine() -> None:
    assert await _check([]) == {}


@pytest.mark.asyncio
async def test_a_destructive_allow_names_the_capability_and_its_class() -> None:
    with pytest.raises(RoutineRejected) as exc:
        await _check([f"{DID}#list_issues", f"{DID}#delete_repo"])
    assert exc.value.field == "scope_allow"
    assert exc.value.reason.startswith("delete_repo is destructive")


@pytest.mark.asyncio
async def test_an_unrecognised_capability_resolves_to_destructive() -> None:
    with pytest.raises(RoutineRejected) as exc:
        await _check([f"{DID}#frobnicate"])
    assert exc.value.field == "scope_allow"
    assert "frobnicate is not in the scope-class table" in exc.value.reason
    assert "resolves to destructive" in exc.value.reason


@pytest.mark.asyncio
async def test_a_manifest_can_tighten_a_class_but_never_loosen_it() -> None:
    tightened = _manifest(scope_classes={"update_label": "destructive"})
    with pytest.raises(RoutineRejected):
        await _check([f"{DID}#update_label"], manifest=tightened)
    loosened = _manifest(scope_classes={"delete_repo": "read"})
    with pytest.raises(RoutineRejected):
        await _check([f"{DID}#delete_repo"], manifest=loosened)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        f"{OTHER}#list_issues",  # not one of this routine's connections
        "list_issues",  # no DID
        f"{DID}#not_exposed_create",  # not a capability of the connection
        f"{DID}#bad id",  # outside AD-17's charset
    ],
)
async def test_an_allow_must_name_a_capability_of_one_of_the_connections(
    entry,
) -> None:
    with pytest.raises(RoutineRejected) as exc:
        await _check([entry])
    assert exc.value.field == "scope_allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connections", "manifests", "fragment"),
    [
        ([], {}, "at least one connection"),
        (["web_search"], {}, "is not a connection"),
        (["did:orcha:agent:../system"], {}, "is not a connection"),
        (["did:orcha:agent:x?y"], {}, "is not a connection"),
        ([DID], {}, "could not be read"),
        ([DID], {DID: _manifest(is_active=False)}, "was removed"),
        ([DID], {DID: _manifest(tags=["mcp"])}, "is not an active connection"),
        (
            [DID],
            {DID: {k: v for k, v in _manifest().items() if k != "is_active"}},
            "is not an active connection",
        ),
    ],
)
async def test_every_connection_must_be_current(connections, manifests, fragment):
    with pytest.raises(RoutineRejected) as exc:
        await _check([], connections=connections, read_manifest=_reader(manifests))
    assert exc.value.field == "connections"
    assert fragment in exc.value.reason


def test_criteria_and_operands_accepted() -> None:
    check_criteria({"citations_required": True}, {"citations_required": {"min": 2}})
    check_criteria({}, {})


@pytest.mark.parametrize(
    ("criteria", "operands", "field"),
    [
        (["citations_required"], {}, "criteria"),
        ({"exit_zero": True}, {}, "criteria"),  # not built at this base
        ({"citations_required": "yes"}, {}, "criteria"),
        ({f"c{i}": True for i in range(MAX_CRITERIA + 1)}, {}, "criteria"),
        ({}, [], "criteria_operands"),
        ({}, {"citations_required": {"min": 2}}, "criteria_operands"),
        ({"citations_required": True}, {"citations_required": 2}, "criteria_operands"),
        (
            {"citations_required": True},
            {"citations_required": {"m": [1]}},
            "criteria_operands",
        ),
        (
            {"citations_required": True},
            {"citations_required": {"m": "café"}},
            "criteria_operands",
        ),
        (
            {"citations_required": True},
            {"citations_required": {"m": "x" * 201}},
            "criteria_operands",
        ),
        (
            {"citations_required": True},
            {"citations_required": {"m": None}},
            "criteria_operands",
        ),
    ],
)
def test_criteria_outside_the_rules_are_refused(criteria, operands, field) -> None:
    with pytest.raises(RoutineRejected) as exc:
        check_criteria(criteria, operands)
    assert exc.value.field == field


# -- the route ----------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi import FastAPI
    from superagent.api import routes as api_routes

    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


def test_route_reads_the_registry_fresh_and_returns_classes(client) -> None:
    get = AsyncMock(return_value=_manifest())
    with patch("superagent.middleware.manifest_cache.MANIFEST_CACHE.get_manifest", get):
        resp = client.post(
            "/routines/validate",
            json={"connections": [DID], "scope_allow": [f"{DID}#create_comment"]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"classes": {f"{DID}#create_comment": "write"}}
    get.assert_awaited_once_with(DID, fresh=True)


def test_route_refuses_a_destructive_allow_with_field_and_reason(client) -> None:
    get = AsyncMock(return_value=_manifest())
    with patch("superagent.middleware.manifest_cache.MANIFEST_CACHE.get_manifest", get):
        resp = client.post(
            "/routines/validate",
            json={"connections": [DID], "scope_allow": [f"{DID}#delete_repo"]},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "scope_allow"
    assert detail["reason"].startswith("delete_repo is destructive")


# -- counts_match (story 2.4) --------------------------------------------------


def test_counts_match_with_its_sources_is_accepted() -> None:
    check_criteria({"counts_match": True}, {"counts_match": dict(COUNTS)})
    without_key = {k: v for k, v in COUNTS.items() if k != "key"}
    check_criteria({"counts_match": True}, {"counts_match": without_key})
    # declared off: no operands needed, no verdict, still in the digest
    check_criteria({"counts_match": False}, {})


@pytest.mark.parametrize(
    ("operands", "fragment"),
    [
        ({}, "needs left and right"),
        ({"counts_match": {"left": COUNTS["left"]}}, "needs left and right"),
        (
            {"counts_match": {"left": COUNTS["left"], "right": COUNTS["right"]}},
            "needs left_path and right_path",
        ),
        ({"counts_match": {**COUNTS, "right_path": ""}}, "needs left_path"),
        ({"counts_match": {**COUNTS, "lefty": "x"}}, "lefty is not an operand"),
        ({"counts_match": {**COUNTS, "left_path": 3}}, "must be a string"),
        ({"counts_match": {**COUNTS, "left": DID}}, "left must be"),
        (
            {"counts_match": {**COUNTS, "left": "did:web:x.example#list_issues"}},
            "left must be",
        ),
        ({"counts_match": {**COUNTS, "right": f"{NOTION}#Bad Cap!"}}, "right must be"),
        (
            {"counts_match": {**COUNTS, "left_path": "total_count"}},
            "JSON Pointer",
        ),
        (
            {
                "counts_match": {
                    **COUNTS,
                    "right": COUNTS["left"],
                    "right_path": "/total_count",
                }
            },
            "always passes",
        ),
        (
            {
                "counts_match": {
                    "left": COUNTS["left"],
                    "right": COUNTS["left"],
                    "left_path": "/n",
                    "right_path": "/n",
                }
            },
            "always passes",
        ),
    ],
)
def test_counts_match_outside_the_rules_is_refused(operands, fragment) -> None:
    with pytest.raises(RoutineRejected) as exc:
        check_criteria({"counts_match": True}, operands)
    assert exc.value.field == "criteria_operands"
    assert fragment in exc.value.reason


def _notion() -> dict[str, Any]:
    return _manifest(
        agent_id=NOTION,
        capabilities=[
            {"capability_id": c}
            for c in ("query_database", "API-post-database-query", "databases")
        ],
    )


async def _check_source(right: str, allow: list[str]) -> dict[str, str]:
    return await _check(
        allow,
        connections=[DID, NOTION],
        criteria={"counts_match": True},
        criteria_operands={"counts_match": {**COUNTS, "right": right}},
        read_manifest=_reader({DID: _manifest(), NOTION: _notion()}),
    )


@pytest.mark.asyncio
async def test_a_write_source_needs_to_be_allowed_without_asking() -> None:
    # Notion's MCP query tool is a POST: it resolves to write, so a firing
    # reads it unattended only when the routine allows it.
    source = f"{NOTION}#API-post-database-query"
    with pytest.raises(RoutineRejected) as exc:
        await _check_source(source, [])
    assert exc.value.field == "criteria_operands"
    assert f"allow {source} without asking" in exc.value.reason

    classes = await _check_source(source, [source])
    assert classes == {source: "write"}


@pytest.mark.asyncio
async def test_a_source_resolving_to_destructive_is_refused() -> None:
    # no recognised verb: resolves to destructive, and can never be allowed
    with pytest.raises(RoutineRejected) as exc:
        await _check_source(f"{NOTION}#databases", [])
    assert exc.value.field == "criteria_operands"
    assert "resolves to destructive" in exc.value.reason


@pytest.mark.asyncio
async def test_a_manifest_tightening_a_source_to_write_is_honoured() -> None:
    notion = {**_notion(), "scope_classes": {"query_database": "write"}}
    with pytest.raises(RoutineRejected) as exc:
        await _check(
            [],
            connections=[DID, NOTION],
            criteria={"counts_match": True},
            criteria_operands={"counts_match": dict(COUNTS)},
            read_manifest=_reader({DID: _manifest(), NOTION: notion}),
        )
    assert exc.value.field == "criteria_operands"
    assert "query_database is a write" in exc.value.reason


@pytest.mark.asyncio
async def test_counts_match_sources_on_the_routines_connections_are_accepted() -> None:
    classes = await _check(
        [f"{DID}#list_issues"],
        connections=[DID, NOTION],
        criteria={"counts_match": True},
        criteria_operands={"counts_match": dict(COUNTS)},
        read_manifest=_reader({DID: _manifest(), NOTION: _notion()}),
    )
    assert classes == {f"{DID}#list_issues": "read"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operands", "fragment"),
    [
        (COUNTS, "does not name one of this routine's connections"),
        (
            {**COUNTS, "right": f"{DID}#query_database"},
            f"query_database is not a capability of {DID}",
        ),
    ],
)
async def test_counts_match_sources_off_the_routine_are_refused(
    operands, fragment
) -> None:
    with pytest.raises(RoutineRejected) as exc:
        await _check(
            [],
            criteria={"counts_match": True},
            criteria_operands={"counts_match": dict(operands)},
        )
    assert exc.value.field == "criteria_operands"
    assert fragment in exc.value.reason


def test_route_refuses_a_counts_match_source_off_the_routine(client) -> None:
    get = AsyncMock(return_value=_manifest())
    with patch("superagent.middleware.manifest_cache.MANIFEST_CACHE.get_manifest", get):
        resp = client.post(
            "/routines/validate",
            json={
                "connections": [DID],
                "scope_allow": [],
                "criteria": {"counts_match": True},
                "criteria_operands": {"counts_match": COUNTS},
            },
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "criteria_operands"
    assert NOTION in detail["reason"]


# -- what a firing says (story 2.5) ---------------------------------------------

_FORBIDDEN = re.compile(r"\b(done|success|verified)\b", re.IGNORECASE)


def test_no_firing_detail_says_done_success_or_verified() -> None:
    from superagent.workflow import firing_rules as rules

    details = [
        rules.OVERLAP,
        rules.RESTART,
        rules.OWNER_REMOVED,
        rules.ATTESTATION_OFF,
        rules.NO_RECEIPT,
        rules.INVALID_SCHEDULE,
        rules.CONNECTION_REVOKED.format(did=DID),
        rules.INTERNAL,
    ]
    assert [d for d in details if _FORBIDDEN.search(d)] == []


def test_the_firing_view_reads_the_gates_verdict_fail() -> None:
    # The scheduler names a refused firing's check by replacing this id; it
    # imports firing_view, never the gate module.
    from superagent.pricing.settle_gate import CHECK_VERDICT_FAIL

    from common.utils.src import firing_view

    assert firing_view.VERDICT_FAIL == CHECK_VERDICT_FAIL


def test_the_pane_knows_every_routine_criterion() -> None:
    # A new criterion must say which signed verdict proves it was evaluated.
    from superagent.middleware.criteria import ROUTINE_CRITERIA

    from common.utils.src import firing_view

    assert set(firing_view.VERDICT_FOR_CRITERION) == set(ROUTINE_CRITERIA)


def test_the_pane_reads_no_check_from_an_envelope_the_gate_could_not_verify() -> None:
    """firing_view.UNTRUSTED_ENVELOPE_CHECKS is exactly the gate's verifier failures."""
    from superagent.pricing import settle_gate

    from common.utils.src import firing_view

    assert (
        set(settle_gate._VERIFIER_CHECK_ORDER)
        | {settle_gate.CHECK_VERIFY_ERROR, settle_gate.CHECK_RUN_ID_MISMATCH}
        == firing_view.UNTRUSTED_ENVELOPE_CHECKS
    )
