"""Offices and membership (story 2.0, PRD FR-33).

An office has owners and members. Owners add existing registered users by
email, change roles and remove members; the last owner can be neither
removed nor demoted, and a personal office never takes a second member.

Removing a member is ordered so that a failure never leaves them acting in
the office with nothing to show for it:

1. their connections in this office are revoked (vault rows, soft delete,
   session copies — the story 1.7 semantics);
2. their routines in this office are paused;
3. only then is the membership deleted.

A failure in step 1 or 2 returns an error and leaves the membership in place;
every step is idempotent, so a retry converges. Their receipts are never
touched and stay valid.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from ..auth.models import TokenPayload
from ..dependencies import require_auth, require_member
from ..plugins.routes import remove_member_connections
from .context import (
    MEMBER,
    OWNER,
    ensure_personal_office,
    personal_office_id,
    resolve_office,
    role_name,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/offices", tags=["offices"])

GUEST_EMAIL_SUFFIX = "@sandbox.orcha.local"
OWNER_ONLY = "only an owner of this office can do that"
LAST_OWNER = "an office must keep at least one owner"
PERSONAL_OFFICE = "a personal office has exactly one member"


class OfficeResponse(BaseModel):
    id: str
    name: str
    role: str
    personal: bool


class CreateOfficeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MemberResponse(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    role: str


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: str = Field(default=MEMBER, pattern="^(OWNER|MEMBER)$")


class UpdateMemberRequest(BaseModel):
    role: str = Field(pattern="^(OWNER|MEMBER)$")


def _office_response(membership: Any) -> OfficeResponse:
    office = membership.office
    return OfficeResponse(
        id=office.id,
        name=office.name,
        role=role_name(membership.role),
        personal=office.personal_owner_id is not None,
    )


def _member_response(membership: Any) -> MemberResponse:
    user = getattr(membership, "user", None)
    return MemberResponse(
        user_id=membership.user_id,
        email=getattr(user, "email", None),
        display_name=getattr(user, "display_name", None),
        role=role_name(membership.role),
    )


async def _owner_count(db: Any, office_id: str) -> int:
    return await db.officemember.count(where={"office_id": office_id, "role": OWNER})


async def _target(db: Any, office_id: str, user_id: str) -> Any:
    found = await db.officemember.find_first(
        where={"office_id": office_id, "user_id": user_id}
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="member not found"
        )
    return found


@router.get("", response_model=list[OfficeResponse])
async def list_offices(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> list[OfficeResponse]:
    db = request.app.state.db
    await ensure_personal_office(db, payload.user_id)
    rows = await db.officemember.find_many(
        where={"user_id": payload.user_id},
        include={"office": True},
        order={"created_at": "asc"},
    )
    return [_office_response(r) for r in rows]


@router.post("", response_model=OfficeResponse, status_code=201)
async def create_office(
    body: CreateOfficeRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> OfficeResponse:
    db = request.app.state.db
    office = await db.office.create(
        data={
            "name": body.name.strip(),
            "members": {"create": {"user_id": payload.user_id, "role": OWNER}},
        }
    )
    return OfficeResponse(id=office.id, name=office.name, role=OWNER, personal=False)


@router.get("/{office_id}/members", response_model=list[MemberResponse])
async def list_members(
    office_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> list[MemberResponse]:
    db = request.app.state.db
    await resolve_office(db, payload, office_id)  # 404 unless a member
    rows = await db.officemember.find_many(
        where={"office_id": office_id},
        include={"user": True},
        order={"created_at": "asc"},
    )
    return [_member_response(r) for r in rows]


@router.post("/{office_id}/members", response_model=MemberResponse, status_code=201)
async def add_member(
    office_id: str,
    body: AddMemberRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> MemberResponse:
    db = request.app.state.db
    ctx = await resolve_office(db, payload, office_id)
    if not ctx.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_ONLY)
    if ctx.is_personal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=PERSONAL_OFFICE
        )
    email = str(body.email).lower()
    user = await db.user.find_unique(where={"email": email})
    if user is None or email.endswith(GUEST_EMAIL_SUFFIX) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no registered account with that email",
        )
    existing = await db.officemember.find_first(
        where={"office_id": office_id, "user_id": user.id}
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already a member"
        )
    created = await db.officemember.create(
        data={"office_id": office_id, "user_id": user.id, "role": body.role},
        include={"user": True},
    )
    return _member_response(created)


@router.patch("/{office_id}/members/{user_id}", response_model=MemberResponse)
async def update_member(
    office_id: str,
    user_id: str,
    body: UpdateMemberRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> MemberResponse:
    db = request.app.state.db
    ctx = await resolve_office(db, payload, office_id)
    if not ctx.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_ONLY)
    target = await _target(db, office_id, user_id)
    if (
        role_name(target.role) == OWNER
        and body.role != OWNER
        and await _owner_count(db, office_id) <= 1
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_OWNER)
    updated = await db.officemember.update(
        where={"id": target.id}, data={"role": body.role}, include={"user": True}
    )
    return _member_response(updated)


@router.delete("/{office_id}/members/{user_id}", status_code=204)
async def remove_member(
    office_id: str,
    user_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> None:
    """Remove a member (an owner), or leave the office (the member themself)."""
    db = request.app.state.db
    ctx = await resolve_office(db, payload, office_id)
    if user_id != ctx.user_id and not ctx.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_ONLY)
    if office_id == personal_office_id(user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=PERSONAL_OFFICE
        )
    target = await _target(db, office_id, user_id)
    if role_name(target.role) == OWNER and await _owner_count(db, office_id) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_OWNER)

    await remove_member_connections(request, office_id, user_id)
    try:
        await db.workflowtemplate.update_many(
            where={"office_id": office_id, "user_id": user_id},
            data={"status": "inactive", "schedule_enabled": False},
        )
    except Exception:
        logger.exception("member removal: routines could not be paused")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="their connections were revoked, but their routines could not be "
            "paused; the member was not removed",
        ) from None
    await db.officemember.delete(where={"id": target.id})
