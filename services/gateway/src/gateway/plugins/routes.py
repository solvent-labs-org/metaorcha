"""User-owned plugin routes — MCP connect without a yaml file."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from common.utils.src.operator import STDIO_OPERATOR_ONLY, is_operator

from ..auth.models import TokenPayload
from ..config import settings
from ..dependencies import require_member
from .mcp_manifest import (
    AUTH_VAR_PATTERN,
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
