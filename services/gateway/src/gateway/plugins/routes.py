"""User-owned plugin routes — MCP connect without a yaml file."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from common.utils.src.operator import STDIO_OPERATOR_ONLY, is_operator

from ..auth.models import TokenPayload
from ..config import settings
from ..dependencies import require_member
from .mcp_manifest import (
    AUTH_VAR_PATTERN,
    CONNECTION_TAG,
    build_mcp_emerge_yaml,
    mint_connection_did,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])

CONNECTIONS_DISABLED = (
    "connections_disabled: connecting a tool is turned off on this deployment "
    "(CONNECTIONS_ENABLED)"
)
# The Registry's register route reads this header for the harvest only.
HARVEST_AUTHORIZATION_HEADER = "X-Harvest-Authorization"
# A connection's DID as the connect route mints it: no ``/``, ``?`` or glob
# character, so it is safe in a SuperAgent path and a Redis SCAN pattern.
_CONNECTION_DID = re.compile(r"did:orcha:agent:[A-Za-z0-9._-]{1,128}")
_SESSION_CRED_PREFIX = "gateway:creds:session:"


class ConnectMcpRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    transport: Literal["sse", "stdio"]
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    auth_var: str | None = Field(default=None, pattern=AUTH_VAR_PATTERN)
    auth_value: str | None = None

    @model_validator(mode="after")
    def _auth_is_a_pair(self) -> ConnectMcpRequest:
        # A value without a name has nowhere to go; a name without a value
        # would advertise a vault ref that is never written. Refuse both.
        if bool(self.auth_var) != bool(self.auth_value):
            raise ValueError("auth_var and auth_value must be given together")
        return self


@router.post("/mcp", status_code=201)
async def connect_mcp(
    body: ConnectMcpRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> Any:
    # AD-18: the whole connections feature sits behind one flag, off by default.
    if not settings.connections_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=CONNECTIONS_DISABLED
        )
    # AD-13: a stdio transport runs a subprocess on our host. Operators only,
    # on this door and on the Registry's — the Registry re-checks.
    if body.transport == "stdio" and not is_operator(payload.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=STDIO_OPERATOR_ONLY
        )
    # AD-15: one DID per registration, minted before anything is stored.
    agent_id = mint_connection_did(body.name)
    try:
        yaml_text = build_mcp_emerge_yaml(
            name=body.name,
            transport=body.transport,
            endpoint=body.endpoint,
            command=body.command,
            args=body.args,
            auth_var=body.auth_var if body.auth_value else None,
            description=body.description,
            did=agent_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # The manifest pins the minted DID, so the vault key is known before the
    # Registry answers. Store the credential FIRST: if the vault refuses,
    # nothing is registered and the caller can simply retry. The old order
    # (register, then vault) returned 201 on a failed vault write and left a
    # registered MCP with no credential behind it.
    if body.auth_value and body.auth_var:
        sa = request.app.state.superagent
        cred = await sa.post(
            "/secrets/agent-env",
            json={
                "user_id": payload.user_id,
                "agent_id": agent_id,
                "credentials": {body.auth_var: body.auth_value},
            },
        )
        if cred.status_code >= 400:
            logger.warning(
                "mcp connect: vault write failed (%s); nothing registered",
                cred.status_code,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="credential could not be stored; the MCP was not registered",
            )

    files = {"emerge_yaml": ("emerge.yaml", yaml_text.encode("utf-8"), "text/yaml")}
    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth
    # A server that lists its tools only to a token holder cannot be harvested
    # without the token. The Registry uses this for the one harvest, the same
    # way the SuperAgent sends it on dispatch (http_bearer), and stores nothing.
    if body.auth_value and body.auth_var:
        headers[HARVEST_AUTHORIZATION_HEADER] = f"Bearer {body.auth_value}"
    resp = await request.app.state.registry.post(
        "/api/v1/agents/register", files=files, headers=headers
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    registered = (data.get("data") or {}).get("agent_id")
    if registered and registered != agent_id:
        # Both ids derive from the caller's name: strip line breaks before logging.
        logger.warning(
            "mcp connect: registry returned %s for manifest id %s",
            str(registered).replace("\r", "").replace("\n", ""),
            agent_id.replace("\r", "").replace("\n", ""),
        )
    return data


def _not_a_connection(agent_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"no connection {agent_id}",
    )


async def _sweep_session_credentials(redis: Any, agent_id: str) -> int:
    """Drop session-scoped tokens stored for this connection, in any session.

    They are plaintext in Redis for up to an hour (AD-14 open item) and grant
    nothing once the connection is gone. Best effort: the revoke has already
    succeeded when this runs.
    """
    removed = 0
    try:
        async for key in redis.scan_iter(match=f"{_SESSION_CRED_PREFIX}*:{agent_id}:*"):
            removed += int(await redis.delete(key) or 0)
    except Exception:
        logger.warning("mcp revoke: session-credential sweep failed", exc_info=True)
    return removed


def _forwarded_auth(request: Request) -> dict[str, str]:
    auth = request.headers.get("authorization")
    return {"authorization": auth} if auth else {}


async def is_connection_record(request: Request, agent_id: str) -> bool:
    """Whether the Registry holds ``agent_id`` as a connection.

    False for an id that is not a connection DID (no Registry read) and for
    a 404. Any other read failure raises 502: a door that cannot tell must
    not guess, or a connection would be removed without its token.
    """
    if not _CONNECTION_DID.fullmatch(agent_id):
        return False
    got = await request.app.state.registry.get(
        f"/api/v1/agents/{agent_id}", headers=_forwarded_auth(request)
    )
    if got.status_code == status.HTTP_404_NOT_FOUND:
        return False
    if got.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the connection could not be read; nothing was removed",
        )
    identity = ((got.json() or {}).get("data") or {}).get("identity") or {}
    return CONNECTION_TAG in (identity.get("tags") or [])


async def remove_connection(request: Request, agent_id: str, user_id: str) -> None:
    """Steps 2-4 of ``revoke_connection``, for a record already known to be one.

    Also called by the Agent Library's generic delete, so both doors that
    remove a connection take its token with it (story 1.7).
    """
    sa = request.app.state.superagent
    cred = await sa.delete(
        f"/secrets/agent-env/{agent_id}", params={"user_id": user_id}
    )
    if cred.status_code >= 400:
        logger.warning(
            "mcp revoke: vault delete failed (%s); nothing deregistered",
            cred.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the token could not be removed; the connection was not removed",
        )

    resp = await request.app.state.registry.delete(
        f"/api/v1/agents/{agent_id}", headers=_forwarded_auth(request)
    )
    if resp.status_code >= 400:
        logger.warning("mcp revoke: registry delete failed (%s)", resp.status_code)
        refused = resp.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )
        raise HTTPException(
            status_code=resp.status_code if refused else status.HTTP_502_BAD_GATEWAY,
            detail=(
                "your token for this connection was removed, but the connection "
                "itself was not deregistered"
            ),
        )

    await _sweep_session_credentials(request.app.state.redis, agent_id)


@router.delete("/mcp/{agent_id}", status_code=204)
async def revoke_connection(
    agent_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> None:
    """Remove a whole connection: the caller's tokens, then the registration.

    Story 1.7 (FR-22, AD-6). Not gated on CONNECTIONS_ENABLED: removing
    access is the safe direction, and a deployment that turned connections
    off must still let a user take their token back.

    Order and failure:

    1. The Registry record must exist and carry the ``connection`` tag, so
       this door removes connections only (any other agent keeps its own
       delete route).
    2. The caller's own vault rows ``agent:<DID>:env:*`` go first. Only the
       caller's rows can match, so another user is never affected, and from
       this moment the next call fails closed with ``credential_missing``.
    3. Then the Registry soft-deletes the agent (owner-checked there). A
       refusal or failure is returned as an error, never a 204; a retry
       converges, because both steps are idempotent.
    4. Session-scoped copies of the token are swept last.

    Receipts already sealed are not touched: nothing here reads or writes an
    attestation, a transcript or an audit row.
    """
    if not await is_connection_record(request, agent_id):
        raise _not_a_connection(agent_id)
    await remove_connection(request, agent_id, payload.user_id)
