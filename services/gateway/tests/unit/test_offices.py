"""Story 2.0: offices — membership, and the boundary every scoped route keeps.

PRD FR-32/33/36, NFR-17, OQ-14..18. A request runs in the office its
``X-Orcha-Office`` header names, or the caller's personal office. An office
the caller is not a member of is 404, never 403. Owners see every routine in
the office and may pause any; members see their own. A connection, a routine
and a session each belong to one office and are invisible from any other.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Request
from httpx import Response as _Response

from .office_db import FakeDB, fail_with

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

OFFICE = "off-1"
OTHER = "off-2"
ALICE_DID = "did:orcha:agent:alice-mcp-0a1b2c3d"
BOB_DID = "did:orcha:agent:bob-mcp-1b2c3d4e"
CAROL_DID = "did:orcha:agent:carol-mcp-2c3d4e5f"


def Response(status_code: int, **kw) -> _Response:  # noqa: N802
    # a request is attached so the routes' raise_for_status() can run
    return _Response(status_code, request=Request("GET", "http://sa.test"), **kw)


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def sismember(self, _key: str, _member: str) -> bool:
        return False  # the JWT revocation set (require_auth)

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def scan_iter(self, match: str):
        prefix, _, rest = match.partition("*")
        needle = rest.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix) and needle in key:
                yield key

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


def _manifest(did: str) -> Response:
    return Response(
        200,
        json={
            "status": "success",
            "data": {"identity": {"id": did, "tags": ["mcp", "connection"]}},
        },
    )


class _Env:
    def __init__(self, ac, db, redis, superagent, registry, tokens) -> None:
        self.ac = ac
        self.db = db
        self.redis = redis
        self.superagent = superagent
        self.registry = registry
        self.tokens = tokens

    def h(self, who: str, office: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.tokens[who]}"}
        if office is not None:
            headers["X-Orcha-Office"] = office
        return headers


@pytest_asyncio.fixture
async def env(monkeypatch):
    from gateway.auth.jwt import create_access_token
    from gateway.config import settings
    from gateway.main import app

    monkeypatch.setattr(settings, "connections_enabled", True)
    db = FakeDB()
    for uid in ("alice", "bob", "carol", "dave"):
        db.add_user(uid)
    db.add_user("guest-1", email="guest-1@sandbox.orcha.local")
    db.add_office(OFFICE, {"alice": "OWNER", "bob": "MEMBER"})
    db.add_office(OTHER, {"carol": "OWNER"})
    db.add_connection(ALICE_DID, "alice", OFFICE)
    db.add_connection(BOB_DID, "bob", OFFICE)
    db.add_connection(CAROL_DID, "carol", OTHER)

    superagent = MagicMock()
    superagent.delete = AsyncMock(return_value=Response(200, json={"deleted": 1}))
    superagent.post = AsyncMock(return_value=Response(200, json={"classes": {}}))
    superagent.get = AsyncMock(return_value=Response(404, json={}))
    registry = MagicMock()
    registry.get = AsyncMock(
        side_effect=lambda path, **_: _manifest(path.rsplit("/", 1)[1])
    )
    registry.delete = AsyncMock(return_value=Response(200, json={"status": "success"}))
    redis = _Redis()
    app.state.db = db
    app.state.redis = redis
    app.state.superagent = superagent
    app.state.registry = registry
    tokens = {
        uid: create_access_token(user_id=uid, email=f"{uid}@example.com")[0]
        for uid in ("alice", "bob", "carol", "dave")
    }
    tokens["guest"] = create_access_token(
        user_id="guest-1", email="guest-1@sandbox.orcha.local", guest=True
    )[0]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield _Env(ac, db, redis, superagent, registry, tokens)


# ── which office a request runs in ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_personal_office_is_made_on_first_use_and_only_once(env) -> None:
    for _ in range(2):
        resp = await env.ac.get("/api/v1/offices", headers=env.h("dave"))
        assert resp.status_code == 200
    assert resp.json() == [
        {"id": "po_dave", "name": "Personal", "role": "OWNER", "personal": True}
    ]
    assert [
        o["id"] for o in env.db.office.rows if o["personal_owner_id"] == "dave"
    ] == ["po_dave"]


@pytest.mark.asyncio
async def test_a_lost_race_for_the_personal_office_is_not_an_error(env) -> None:
    from gateway.offices.context import ensure_personal_office

    env.db.add_personal_office("dave")  # a concurrent first request won
    real = env.db.officemember.find_first
    reads: list[dict] = []

    async def racing(where, **kw):
        reads.append(where)
        if len(reads) == 1:
            return None  # our first read ran before the winner's commit
        return await real(where, **kw)

    env.db.officemember.find_first = racing
    await ensure_personal_office(env.db, "dave")  # create hits the unique key
    assert sum(1 for o in env.db.office.rows if o["personal_owner_id"] == "dave") == 1
    assert [c[0] for c in env.db.office.calls] == ["create"]
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_a_personal_office_that_cannot_be_made_is_503(env) -> None:
    from fastapi import HTTPException

    from gateway.offices.context import ensure_personal_office

    fail_with(env.db.office, "create")
    with pytest.raises(HTTPException) as err:
        await ensure_personal_office(env.db, "dave")
    assert err.value.status_code == 503


@pytest.mark.asyncio
async def test_an_office_you_are_not_in_is_404_never_403(env) -> None:
    for path in ("/api/v1/workflows", "/api/v1/sessions"):
        resp = await env.ac.get(path, headers=env.h("bob", OTHER))
        assert resp.status_code == 404, path
    resp = await env.ac.get("/api/v1/workflows", headers=env.h("bob", "no-such-office"))
    assert resp.status_code == 404
    resp = await env.ac.get(f"/api/v1/offices/{OTHER}/members", headers=env.h("bob"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_offices_shows_each_membership_with_its_role(env) -> None:
    resp = await env.ac.get("/api/v1/offices", headers=env.h("bob"))
    got = {o["id"]: (o["role"], o["personal"]) for o in resp.json()}
    assert got == {"po_bob": ("OWNER", True), OFFICE: ("MEMBER", False)}


# ── membership ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creating_an_office_makes_the_caller_its_owner(env) -> None:
    resp = await env.ac.post(
        "/api/v1/offices", json={"name": " Ops "}, headers=env.h("dave")
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Ops" and body["role"] == "OWNER" and not body["personal"]
    assert env.db.role_of(body["id"], "dave") == "OWNER"


@pytest.mark.asyncio
async def test_a_guest_can_neither_create_an_office_nor_be_added(env) -> None:
    resp = await env.ac.post(
        "/api/v1/offices", json={"name": "x"}, headers=env.h("guest")
    )
    assert resp.status_code == 403
    resp = await env.ac.post(
        f"/api/v1/offices/{OFFICE}/members",
        json={"email": "guest-1@sandbox.orcha.local"},
        headers=env.h("alice", OFFICE),
    )
    # the address is refused as a special-use domain before the guest check runs
    assert resp.status_code in (404, 422)
    assert env.db.role_of(OFFICE, "guest-1") is None
    env.db.user.rows[2]["is_active"] = False  # carol, deactivated
    resp = await env.ac.post(
        f"/api/v1/offices/{OFFICE}/members",
        json={"email": "carol@example.com"},
        headers=env.h("alice", OFFICE),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_owner_adds_a_registered_user_by_email(env) -> None:
    url = f"/api/v1/offices/{OFFICE}/members"
    resp = await env.ac.post(
        url, json={"email": "Dave@Example.com"}, headers=env.h("alice")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "MEMBER"
    assert env.db.role_of(OFFICE, "dave") == "MEMBER"
    again = await env.ac.post(
        url, json={"email": "dave@example.com"}, headers=env.h("alice")
    )
    assert again.status_code == 409
    nobody = await env.ac.post(
        url, json={"email": "nobody@example.com"}, headers=env.h("alice")
    )
    assert nobody.status_code == 404


@pytest.mark.asyncio
async def test_only_an_owner_adds_or_changes_members(env) -> None:
    url = f"/api/v1/offices/{OFFICE}/members"
    resp = await env.ac.post(
        url, json={"email": "dave@example.com"}, headers=env.h("bob")
    )
    assert resp.status_code == 403
    resp = await env.ac.patch(
        f"{url}/alice", json={"role": "MEMBER"}, headers=env.h("bob")
    )
    assert resp.status_code == 403
    assert env.db.role_of(OFFICE, "dave") is None
    assert env.db.role_of(OFFICE, "alice") == "OWNER"


@pytest.mark.asyncio
async def test_a_personal_office_takes_no_second_member(env) -> None:
    await env.ac.get("/api/v1/offices", headers=env.h("dave"))
    resp = await env.ac.post(
        "/api/v1/offices/po_dave/members",
        json={"email": "bob@example.com"},
        headers=env.h("dave"),
    )
    assert resp.status_code == 409
    resp = await env.ac.delete(
        "/api/v1/offices/po_dave/members/dave", headers=env.h("dave")
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_the_last_owner_can_be_neither_demoted_nor_removed(env) -> None:
    url = f"/api/v1/offices/{OFFICE}/members/alice"
    resp = await env.ac.patch(url, json={"role": "MEMBER"}, headers=env.h("alice"))
    assert resp.status_code == 409
    resp = await env.ac.delete(url, headers=env.h("alice"))
    assert resp.status_code == 409
    # with a second owner, the first may step down
    await env.ac.patch(
        f"/api/v1/offices/{OFFICE}/members/bob",
        json={"role": "OWNER"},
        headers=env.h("alice"),
    )
    resp = await env.ac.patch(url, json={"role": "MEMBER"}, headers=env.h("alice"))
    assert resp.status_code == 200
    assert env.db.role_of(OFFICE, "alice") == "MEMBER"


@pytest.mark.asyncio
async def test_a_member_may_leave_but_not_remove_another(env) -> None:
    env.db.add_user("erin")
    env.db.officemember.insert(office_id=OFFICE, user_id="erin", role="MEMBER")
    resp = await env.ac.delete(
        f"/api/v1/offices/{OFFICE}/members/erin", headers=env.h("bob")
    )
    assert resp.status_code == 403
    resp = await env.ac.delete(
        f"/api/v1/offices/{OFFICE}/members/bob", headers=env.h("bob")
    )
    assert resp.status_code == 204
    assert env.db.role_of(OFFICE, "bob") is None


# ── removing a member: connections, then routines, then the membership ────


def _seed_routine(db: FakeDB, rid: str, user_id: str, office_id: str, **extra) -> None:
    db.workflowtemplate.insert(
        id=rid,
        user_id=user_id,
        office_id=office_id,
        name=rid,
        description=None,
        goal_template="g",
        agents_used=[],
        steps=[],
        parameters={},
        **extra,
    )


@pytest.mark.asyncio
async def test_removal_revokes_connections_then_pauses_routines_then_removes(
    env,
) -> None:
    env.redis.store[f"gateway:creds:session:s-1:{BOB_DID}:TOKEN"] = "x"
    env.redis.store[f"gateway:creds:session:s-1:{ALICE_DID}:TOKEN"] = "y"
    env.db.add_connection("did:orcha:agent:bob-elsewhere-99", "bob", "po_bob")
    _seed_routine(env.db, "wf-b", "bob", OFFICE, status="active", schedule_enabled=True)
    _seed_routine(
        env.db, "wf-a", "alice", OFFICE, status="active", schedule_enabled=True
    )

    resp = await env.ac.delete(
        f"/api/v1/offices/{OFFICE}/members/bob", headers=env.h("alice")
    )
    assert resp.status_code == 204, resp.text

    # bob's token for his connection in THIS office is gone, as bob, and nothing else
    env.superagent.delete.assert_awaited_once_with(
        f"/secrets/agent-env/{BOB_DID}", params={"user_id": "bob"}
    )
    agents = {a["id"]: a["is_active"] for a in env.db.agent.rows}
    assert agents[BOB_DID] is False
    assert agents[ALICE_DID] is True
    assert agents["did:orcha:agent:bob-elsewhere-99"] is True
    assert list(env.redis.store) == [f"gateway:creds:session:s-1:{ALICE_DID}:TOKEN"]
    env.registry.delete.assert_not_awaited()  # the Registry's DELETE checks the caller
    routines = {
        w["id"]: (w["status"], w["schedule_enabled"])
        for w in env.db.workflowtemplate.rows
    }
    assert routines == {"wf-b": ("inactive", False), "wf-a": ("active", True)}
    assert env.db.role_of(OFFICE, "bob") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["vault", "soft_delete", "pause"])
async def test_a_failed_removal_step_keeps_the_membership(env, broken) -> None:
    _seed_routine(env.db, "wf-b", "bob", OFFICE, status="active", schedule_enabled=True)
    if broken == "vault":
        env.superagent.delete.return_value = Response(500, text="down")
    elif broken == "soft_delete":
        fail_with(env.db.agent, "update")
    else:
        fail_with(env.db.workflowtemplate, "update_many")
    resp = await env.ac.delete(
        f"/api/v1/offices/{OFFICE}/members/bob", headers=env.h("alice")
    )
    assert resp.status_code == 502
    assert env.db.role_of(OFFICE, "bob") == "MEMBER"
    if broken == "vault":
        assert env.db.agent.get(BOB_DID).is_active is True  # nothing past the failure


# ── routines: visibility and who may change what ───────────────────────────


@pytest.mark.asyncio
async def test_a_member_sees_their_own_routines_an_owner_sees_all(env) -> None:
    _seed_routine(env.db, "wf-a", "alice", OFFICE, status="inactive")
    _seed_routine(env.db, "wf-b", "bob", OFFICE, status="inactive")
    _seed_routine(env.db, "wf-c", "carol", OTHER, status="inactive")
    _seed_routine(env.db, "wf-p", "bob", "po_bob", status="inactive")

    def ids(resp):
        return sorted(w["id"] for w in resp.json())

    assert ids(await env.ac.get("/api/v1/workflows", headers=env.h("bob", OFFICE))) == [
        "wf-b"
    ]
    assert ids(
        await env.ac.get("/api/v1/workflows", headers=env.h("alice", OFFICE))
    ) == [
        "wf-a",
        "wf-b",
    ]
    assert ids(await env.ac.get("/api/v1/workflows", headers=env.h("bob"))) == ["wf-p"]
    for rid in ("wf-a", "wf-c"):
        resp = await env.ac.get(
            f"/api/v1/workflows/{rid}", headers=env.h("bob", OFFICE)
        )
        assert resp.status_code == 404, rid
    # a routine is invisible from any other office, even to its own author
    resp = await env.ac.get("/api/v1/workflows/wf-b", headers=env.h("bob"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_owner_may_pause_a_members_routine_but_not_edit_or_delete_it(
    env,
) -> None:
    _seed_routine(env.db, "wf-b", "bob", OFFICE, status="active", schedule_enabled=True)
    url = "/api/v1/workflows/wf-b"
    edit = await env.ac.patch(
        url, json={"name": "mine now"}, headers=env.h("alice", OFFICE)
    )
    assert edit.status_code == 403
    gone = await env.ac.delete(url, headers=env.h("alice", OFFICE))
    assert gone.status_code == 403
    pause = await env.ac.patch(
        url, json={"status": "inactive"}, headers=env.h("alice", OFFICE)
    )
    assert pause.status_code == 200, pause.text
    row = env.db.workflowtemplate.get("wf-b")
    assert (row.name, row.status, row.schedule_enabled) == ("wf-b", "inactive", False)
    # and the author still owns it
    resp = await env.ac.delete(url, headers=env.h("bob", OFFICE))
    assert resp.status_code == 204


def _routine(**over) -> dict:
    body = {
        "name": "Weekly",
        "goal": "Summarise",
        "connections": [BOB_DID],
        "scope_allow": [],
        "model": "m",
        "criteria": {},
        "criteria_operands": {},
        "cron": "0 9 * * 1",
        "timezone": "UTC",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "did",
    [ALICE_DID, CAROL_DID, "did:orcha:agent:nobody-00000000"],
    ids=["colleague", "other-office", "unknown"],
)
async def test_a_routine_uses_only_your_own_connections_in_this_office(
    env, did
) -> None:
    resp = await env.ac.post(
        "/api/v1/workflows/routines",
        json=_routine(connections=[BOB_DID, did]),
        headers=env.h("bob", OFFICE),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "connections"
    assert did in resp.json()["detail"]["reason"]
    env.superagent.post.assert_not_awaited()
    assert env.db.workflowtemplate.rows == []


@pytest.mark.asyncio
async def test_a_routine_is_saved_into_the_request_office(env) -> None:
    resp = await env.ac.post(
        "/api/v1/workflows/routines", json=_routine(), headers=env.h("bob", OFFICE)
    )
    assert resp.status_code == 201, resp.text
    (row,) = env.db.workflowtemplate.rows
    assert (row["office_id"], row["user_id"]) == (OFFICE, "bob")
    # a revoked connection no longer counts
    env.db.agent.rows[1]["is_active"] = False
    again = await env.ac.post(
        "/api/v1/workflows/routines", json=_routine(), headers=env.h("bob", OFFICE)
    )
    assert again.status_code == 422


# ── connections ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_connection_is_bound_to_the_request_office(env) -> None:
    did = "did:orcha:agent:docs-12345678"

    async def register(*_a, **_k):
        env.db.agent.insert(id=did, user_id="bob", tags=["mcp", "connection"])
        return Response(201, json={"status": "success", "data": {"agent_id": did}})

    env.registry.post = AsyncMock(side_effect=register)
    resp = await env.ac.post(
        "/api/v1/plugins/mcp",
        json={
            "name": "Docs",
            "transport": "sse",
            "endpoint": "https://example.com/mcp",
        },
        headers=env.h("bob", OFFICE),
    )
    assert resp.status_code == 201, resp.text
    assert env.db.agent.get(did).office_id == OFFICE


@pytest.mark.asyncio
async def test_a_connection_that_cannot_be_bound_is_not_kept(env) -> None:
    did = "did:orcha:agent:docs-12345678"
    env.registry.post = AsyncMock(
        return_value=Response(
            201, json={"status": "success", "data": {"agent_id": did}}
        )
    )
    fail_with(env.db.agent, "update")
    resp = await env.ac.post(
        "/api/v1/plugins/mcp",
        json={
            "name": "Docs",
            "transport": "sse",
            "endpoint": "https://example.com/mcp",
        },
        headers=env.h("bob", OFFICE),
    )
    assert resp.status_code == 502
    env.superagent.delete.assert_awaited_once_with(
        f"/secrets/agent-env/{did}", params={"user_id": "bob"}
    )
    assert env.registry.delete.await_args.args == (f"/api/v1/agents/{did}",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "office", "did"),
    [
        ("bob", "po_bob", BOB_DID),  # his own, but named from another office
        ("alice", OFFICE, BOB_DID),  # an owner, but not hers
        ("carol", OTHER, BOB_DID),  # another office entirely
    ],
)
async def test_a_connection_is_revoked_only_by_its_owner_in_its_office(
    env, who, office, did
) -> None:
    for path in (f"/api/v1/plugins/mcp/{did}", f"/api/v1/dev/agents/{did}"):
        resp = await env.ac.delete(path, headers=env.h(who, office))
        assert resp.status_code == 404, path
    env.superagent.delete.assert_not_awaited()
    env.registry.delete.assert_not_awaited()
    resp = await env.ac.delete(
        f"/api/v1/plugins/mcp/{did}", headers=env.h("bob", OFFICE)
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_a_connection_from_before_offices_stays_revocable_by_its_owner(
    env,
) -> None:
    did = "did:orcha:agent:legacy-abcdef12"
    env.db.add_connection(did, "bob", None)
    resp = await env.ac.delete(f"/api/v1/plugins/mcp/{did}", headers=env.h("bob"))
    assert resp.status_code == 204


# ── sessions ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_session_is_created_in_the_request_office(env) -> None:
    env.superagent.post = AsyncMock(
        return_value=Response(200, json={"session_id": "s-1"})
    )
    resp = await env.ac.post("/api/v1/sessions", json={}, headers=env.h("bob", OFFICE))
    assert resp.status_code == 201, resp.text
    assert env.superagent.post.await_args.kwargs["json"]["office_id"] == OFFICE
    assert env.redis.store["gateway:session-office:s-1"] == OFFICE
    assert env.redis.store["gateway:session:s-1"] == "bob"


@pytest.mark.asyncio
async def test_a_session_is_404_from_any_other_office(env) -> None:
    env.redis.store["gateway:session:s-1"] = "bob"
    env.redis.store["gateway:session-office:s-1"] = OFFICE
    env.superagent.get = AsyncMock(
        return_value=Response(200, json={"session_id": "s-1", "status": "ready"})
    )
    resp = await env.ac.get("/api/v1/sessions/s-1/status", headers=env.h("bob"))
    assert resp.status_code == 404
    resp = await env.ac.get("/api/v1/sessions/s-1/status", headers=env.h("bob", OFFICE))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_a_session_with_no_office_belongs_to_the_personal_office(env) -> None:
    env.redis.store["gateway:session:s-old"] = "bob"
    env.superagent.get = AsyncMock(
        side_effect=lambda path, **_: Response(
            200,
            json=(
                {"session_id": "s-old", "user_id": "bob", "office_id": None}
                if path == "/sessions/s-old"
                else {"session_id": "s-old", "status": "ready"}
            ),
        )
    )
    resp = await env.ac.get(
        "/api/v1/sessions/s-old/status", headers=env.h("bob", OFFICE)
    )
    assert resp.status_code == 404
    assert env.redis.store["gateway:session-office:s-old"] == "po_bob"
    resp = await env.ac.get("/api/v1/sessions/s-old/status", headers=env.h("bob"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_listing_sessions_names_the_office(env) -> None:
    env.superagent.get = AsyncMock(
        return_value=Response(
            200,
            json={
                "items": [],
                "total": 0,
                "page": 1,
                "page_size": 20,
                "has_next": False,
            },
        )
    )
    await env.ac.get("/api/v1/sessions", headers=env.h("bob", OFFICE))
    params = env.superagent.get.await_args.kwargs["params"]
    assert (params["office_id"], params["include_unassigned"]) == (OFFICE, False)
    await env.ac.get("/api/v1/sessions", headers=env.h("bob"))
    params = env.superagent.get.await_args.kwargs["params"]
    assert (params["office_id"], params["include_unassigned"]) == ("po_bob", True)


@pytest.mark.asyncio
async def test_saving_a_chat_workflow_needs_your_session_in_this_office(env) -> None:
    # before story 2.0 any session id's captured workflow could be read and saved
    env.redis.store["gateway:session:s-c"] = "carol"
    env.redis.store["gateway:session-office:s-c"] = OTHER
    env.superagent.get = AsyncMock(
        return_value=Response(200, json={"captured_workflow": {"goal_template": "g"}})
    )
    resp = await env.ac.post(
        "/api/v1/workflows",
        json={"session_id": "s-c", "name": "x"},
        headers=env.h("bob"),
    )
    assert resp.status_code in (403, 404)
    env.superagent.get.assert_not_awaited()
    assert env.db.workflowtemplate.rows == []

    env.redis.store["gateway:session:s-b"] = "bob"
    env.redis.store["gateway:session-office:s-b"] = OFFICE
    resp = await env.ac.post(
        "/api/v1/workflows",
        json={"session_id": "s-b", "name": "x"},
        headers=env.h("bob", OFFICE),
    )
    assert resp.status_code == 201, resp.text
    (row,) = env.db.workflowtemplate.rows
    assert (row["office_id"], row["user_id"]) == (OFFICE, "bob")
