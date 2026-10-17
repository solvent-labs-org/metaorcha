"""Which office a request runs in (story 2.0, PRD FR-32/33/36, NFR-17).

Every office-scoped route depends on ``require_office``. A request names its
office in the ``X-Orcha-Office`` header; with no header it runs in the
caller's personal office, created on first use. An office the caller is not
a member of answers 404, never 403, so a request cannot learn which office
ids exist.

The personal office has a fixed id, ``po_<user id>`` — the same id the
2026-10-02 migration backfills — and ``offices.personal_owner_id`` is unique,
so two concurrent first requests cannot create two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from ..auth.models import TokenPayload
from ..dependencies import require_auth, require_member

logger = logging.getLogger(__name__)

OFFICE_HEADER = "X-Orcha-Office"
OWNER = "OWNER"
MEMBER = "MEMBER"
OFFICE_NOT_FOUND = "office not found"


def personal_office_id(user_id: str) -> str:
    return f"po_{user_id}"


def role_name(role: Any) -> str:
    """Prisma hands an enum back as an enum member or a plain string."""
    return str(getattr(role, "value", role)).upper()


@dataclass(frozen=True)
class OfficeContext:
    office_id: str
    role: str
    payload: TokenPayload

    @property
    def user_id(self) -> str:
        return self.payload.user_id

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    @property
    def is_personal(self) -> bool:
        return self.office_id == personal_office_id(self.user_id)


async def ensure_personal_office(db: Any, user_id: str) -> None:
    """Create the caller's personal office and owner membership if missing."""
    office_id = personal_office_id(user_id)
    found = await db.officemember.find_first(
        where={"office_id": office_id, "user_id": user_id}
    )
    if found is not None:
        return
    try:
        await db.office.create(
            data={
                "id": office_id,
                "name": "Personal",
                "personal_owner_id": user_id,
                "members": {"create": {"user_id": user_id, "role": OWNER}},
            }
        )
    except Exception:
        # A concurrent first request won the race; the unique keys make the
        # loser fail here. Anything else must still end with the row present.
        found = await db.officemember.find_first(
            where={"office_id": office_id, "user_id": user_id}
        )
        if found is None:
            logger.exception("personal office could not be created")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="your office could not be prepared; try again",
            ) from None


async def resolve_office(
    db: Any, payload: TokenPayload, requested: str | None
) -> OfficeContext:
    office_id = (requested or "").strip() or personal_office_id(payload.user_id)
    if office_id == personal_office_id(payload.user_id):
        await ensure_personal_office(db, payload.user_id)
    membership = await db.officemember.find_first(
        where={"office_id": office_id, "user_id": payload.user_id}
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=OFFICE_NOT_FOUND
        )
    return OfficeContext(
        office_id=office_id, role=role_name(membership.role), payload=payload
    )


async def require_office(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
    x_orcha_office: Annotated[str | None, Header(alias=OFFICE_HEADER)] = None,
) -> OfficeContext:
    return await resolve_office(request.app.state.db, payload, x_orcha_office)


async def require_member_office(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
    x_orcha_office: Annotated[str | None, Header(alias=OFFICE_HEADER)] = None,
) -> OfficeContext:
    """``require_office`` for actions a guest may not take (AD-13)."""
    return await resolve_office(request.app.state.db, payload, x_orcha_office)


async def is_member(db: Any, office_id: str, user_id: str) -> bool:
    if office_id == personal_office_id(user_id):
        return True  # created on first use; always the owner's
    found = await db.officemember.find_first(
        where={"office_id": office_id, "user_id": user_id}
    )
    return found is not None
