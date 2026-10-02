"""An in-memory stand-in for the Prisma tables story 2.0 reads and writes.

Not a Prisma emulator: it supports exactly the query shapes the Gateway uses
— equality, ``None``, ``{"in": [...]}``, ``{"has": x}`` and a top-level
``"OR"`` — and the two ``include`` relations on ``officemember``. A query
shape it does not know raises, so a route that starts using one fails here
instead of passing against a mock that accepts anything.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

_ids = itertools.count(1)


def _matches(row: dict[str, Any], where: dict[str, Any]) -> bool:
    for key, want in where.items():
        if key == "OR":
            if not any(_matches(row, alt) for alt in want):
                return False
            continue
        have = row.get(key)
        if isinstance(want, dict):
            ((op, arg),) = want.items()
            if op == "in":
                if have not in arg:
                    return False
            elif op == "has":
                if arg not in (have or []):
                    return False
            else:
                raise AssertionError(f"office_db: unsupported operator {op!r}")
        elif have != want:
            return False
    return True


def _unwrap(value: Any) -> Any:
    return getattr(value, "data", value)  # prisma Json → plain value


class Table:
    def __init__(
        self,
        db: FakeDB,
        name: str,
        unique: tuple[tuple[str, ...], ...] = (),
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.name = name
        self.rows: list[dict[str, Any]] = []
        self.unique = unique
        self.defaults = defaults or {}
        self.fail: dict[str, Exception] = {}  # method name → exception to raise
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _check(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        if method in self.fail:
            raise self.fail[method]

    def _out(self, row: dict[str, Any], include: dict[str, bool] | None) -> Any:
        out = dict(row)
        for rel in include or {}:
            if rel == "office":
                out["office"] = self.db.office.get(row["office_id"])
            elif rel == "user":
                out["user"] = self.db.user.get(row["user_id"])
            else:
                raise AssertionError(f"office_db: unsupported include {rel!r}")
        return SimpleNamespace(**out)

    def get(self, row_id: str) -> Any:
        for row in self.rows:
            if row["id"] == row_id:
                return SimpleNamespace(**row)
        return None

    def insert(self, **data: Any) -> dict[str, Any]:
        now = datetime(2026, 10, 2, tzinfo=UTC)
        row = {
            "id": f"{self.name}-{next(_ids)}",
            "created_at": now,
            "updated_at": now,
            **self.defaults,
        }
        row.update({k: _unwrap(v) for k, v in data.items()})
        for cols in self.unique:
            if any(row.get(c) is None for c in cols):
                continue
            for other in self.rows:
                if all(other.get(c) == row[c] for c in cols):
                    raise RuntimeError(f"unique constraint {self.name}{cols}")
        self.rows.append(row)
        return row

    async def find_first(self, where: dict[str, Any], **kw: Any) -> Any:
        self._check("find_first", where=where)
        for row in self.rows:
            if _matches(row, where):
                return self._out(row, kw.get("include"))
        return None

    async def find_unique(self, where: dict[str, Any], **kw: Any) -> Any:
        return await self.find_first(where, **kw)

    async def find_many(self, where: dict[str, Any] | None = None, **kw: Any) -> list:
        self._check("find_many", where=where)
        return [
            self._out(r, kw.get("include"))
            for r in self.rows
            if _matches(r, where or {})
        ]

    async def count(self, where: dict[str, Any] | None = None) -> int:
        self._check("count", where=where)
        return sum(1 for r in self.rows if _matches(r, where or {}))

    async def create(self, data: dict[str, Any], **kw: Any) -> Any:
        self._check("create", data=data)
        data = dict(data)
        nested = data.pop("members", None)
        row = self.insert(**data)
        if nested is not None:  # office.create(members={"create": {...}})
            try:
                self.db.officemember.insert(office_id=row["id"], **nested["create"])
            except Exception:
                self.rows.remove(row)  # one transaction, as in Prisma
                raise
        return self._out(row, kw.get("include"))

    async def update(
        self, where: dict[str, Any], data: dict[str, Any], **kw: Any
    ) -> Any:
        self._check("update", where=where, data=data)
        for row in self.rows:
            if _matches(row, where):
                row.update({k: _unwrap(v) for k, v in data.items()})
                return self._out(row, kw.get("include"))
        raise RuntimeError(f"{self.name}: record to update not found")

    async def update_many(self, where: dict[str, Any], data: dict[str, Any]) -> int:
        self._check("update_many", where=where, data=data)
        hit = [r for r in self.rows if _matches(r, where)]
        for row in hit:
            row.update({k: _unwrap(v) for k, v in data.items()})
        return len(hit)

    async def delete(self, where: dict[str, Any]) -> Any:
        self._check("delete", where=where)
        for row in self.rows:
            if _matches(row, where):
                self.rows.remove(row)
                return SimpleNamespace(**row)
        raise RuntimeError(f"{self.name}: record to delete not found")


class FakeDB:
    def __init__(self) -> None:
        self.user = Table(
            self, "user", unique=(("email",),), defaults={"is_active": True}
        )
        self.office = Table(
            self,
            "office",
            unique=(("personal_owner_id",),),
            defaults={"personal_owner_id": None},
        )
        self.officemember = Table(
            self,
            "officemember",
            unique=(("office_id", "user_id"),),
            defaults={"role": "MEMBER"},
        )
        self.agent = Table(
            self, "agent", defaults={"is_active": True, "tags": [], "office_id": None}
        )
        self.workflowtemplate = Table(
            self,
            "workflowtemplate",
            defaults={
                "status": "inactive",
                "schedule_enabled": False,
                "run_count": 0,
                "trigger_type": "manual",
            },
        )

    # ── seeding helpers ────────────────────────────────────────────────────

    def add_user(self, user_id: str, email: str | None = None, **extra: Any) -> None:
        self.user.insert(id=user_id, email=email or f"{user_id}@example.com", **extra)

    def add_personal_office(self, user_id: str) -> str:
        office_id = f"po_{user_id}"
        self.office.insert(id=office_id, name="Personal", personal_owner_id=user_id)
        self.officemember.insert(office_id=office_id, user_id=user_id, role="OWNER")
        return office_id

    def add_office(self, office_id: str, members: dict[str, str]) -> str:
        self.office.insert(id=office_id, name=office_id, personal_owner_id=None)
        for user_id, role in members.items():
            self.officemember.insert(office_id=office_id, user_id=user_id, role=role)
        return office_id

    def add_connection(self, did: str, user_id: str, office_id: str | None) -> None:
        self.agent.insert(
            id=did, user_id=user_id, office_id=office_id, tags=["mcp", "connection"]
        )

    def role_of(self, office_id: str, user_id: str) -> str | None:
        for row in self.officemember.rows:
            if row["office_id"] == office_id and row["user_id"] == user_id:
                return row["role"]
        return None


def fail_with(table: Table, method: str) -> Callable[[], None]:
    """Make ``table.method`` raise until the returned undo is called."""
    table.fail[method] = RuntimeError(f"{table.name}.{method} down")
    return lambda: table.fail.pop(method, None)
