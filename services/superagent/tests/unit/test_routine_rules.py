"""Story 2.1: what a routine may hold, decided before the Gateway persists it.

``check_routine`` and ``POST /routines/validate``: connections must be current
connections, every allow names one of them and a capability it exposes, and
no allow may resolve to ``destructive`` — including a capability the class
rules do not recognise (AD-18: unknown resolves to destructive). Criteria use
the chat path's vocabulary; operands are short printable ASCII (AD-19).
"""

from __future__ import annotations

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
        ({"counts_match": True}, {}, "criteria"),  # not evaluated yet (story 2.3)
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
