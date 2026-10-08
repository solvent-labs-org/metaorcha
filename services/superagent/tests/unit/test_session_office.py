"""Story 2.0: a session is fixed to the office it was created in.

The Gateway decides the office and passes it; the SuperAgent stores it at
creation, never moves it on a later upsert, returns it on ``GET
/sessions/{id}`` and filters the list by it. A session with no office (one
created before offices existed) is returned with ``office_id: null``; the
Gateway reads that as the owner's personal office.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

STORE = "superagent.persistence.transcript_store"


@pytest.fixture
def client() -> TestClient:
    from fastapi import FastAPI
    from superagent.api import routes as api_routes

    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


class _Sessions:
    """The ``conversationsession`` table, for the queries these helpers make."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.wheres: list[dict[str, Any]] = []

    async def upsert(self, where: dict, data: dict) -> None:
        sid = where["id"]
        if sid in self.rows:
            self.rows[sid].update(data["update"])
        else:
            self.rows[sid] = {"office_id": None, **data["create"]}

    async def find_unique(self, where: dict) -> Any:
        row = self.rows.get(where["id"])
        return SimpleNamespace(persisted_message_count=0, **row) if row else None

    async def count(self, where: dict) -> int:
        self.wheres.append(where)
        return 0

    async def find_many(self, where: dict, **_: Any) -> list:
        self.wheres.append(where)
        return []


@pytest.fixture
def table(monkeypatch) -> _Sessions:
    sessions = _Sessions()

    class _Prisma:
        conversationsession = sessions

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    # CI's SuperAgent job does not generate the Prisma client: stand it in.
    module = types.ModuleType("src.generated_client")
    module.Prisma = _Prisma  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.generated_client", module)
    return sessions


@pytest.mark.asyncio
async def test_the_office_is_set_at_creation_and_never_moved(table) -> None:
    from superagent.persistence.transcript_store import upsert_conversation_session

    await upsert_conversation_session("s-1", "bob", "First", "off-1")
    await upsert_conversation_session("s-1", "bob", "Renamed", "off-2")
    assert table.rows["s-1"]["office_id"] == "off-1"
    assert table.rows["s-1"]["title"] == "Renamed"
    await upsert_conversation_session("s-2", "bob", None)  # the runner's call shape
    assert table.rows["s-2"]["office_id"] is None


@pytest.mark.asyncio
async def test_the_detail_carries_the_office_or_null(table) -> None:
    from superagent.persistence.transcript_store import get_session_office

    table.rows["s-1"] = {"user_id": "bob", "office_id": "off-1"}
    table.rows["s-old"] = {"user_id": "bob", "office_id": None}
    assert await get_session_office("s-1") == ("bob", "off-1")
    assert await get_session_office("s-old") == ("bob", None)
    assert await get_session_office("missing") is None


@pytest.mark.asyncio
async def test_the_list_filters_by_office(table) -> None:
    from superagent.persistence.transcript_store import list_sessions_paginated

    await list_sessions_paginated("bob", 1, 20)
    await list_sessions_paginated("bob", 1, 20, "off-1")
    await list_sessions_paginated("bob", 1, 20, "po_bob", include_unassigned=True)
    assert table.wheres[::2] == [
        {"user_id": "bob"},
        {"user_id": "bob", "office_id": "off-1"},
        {"user_id": "bob", "OR": [{"office_id": "po_bob"}, {"office_id": None}]},
    ]


def test_create_passes_the_office_through(client) -> None:
    with patch(f"{STORE}.upsert_conversation_session", new=AsyncMock()) as upsert:
        resp = client.post("/sessions", json={"user_id": "bob", "office_id": "off-1"})
    assert resp.status_code == 200
    args = upsert.await_args.args
    assert (args[1], args[3]) == ("bob", "off-1")


def test_get_session_returns_the_office(client) -> None:
    found = AsyncMock(return_value=("bob", None))
    with patch(f"{STORE}.get_session_office", new=found):
        resp = client.get("/sessions/s-old")
    assert resp.json() == {"session_id": "s-old", "user_id": "bob", "office_id": None}


def test_list_forwards_the_office_filter(client) -> None:
    rows = AsyncMock(return_value=([], 0))
    with patch(f"{STORE}.list_sessions_paginated", new=rows):
        resp = client.get(
            "/sessions",
            params={
                "user_id": "bob",
                "office_id": "po_bob",
                "include_unassigned": True,
            },
        )
    assert resp.status_code == 200
    assert rows.await_args.args == ("bob", 1, 20, "po_bob", True)
