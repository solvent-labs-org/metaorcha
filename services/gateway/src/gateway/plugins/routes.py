"""User-owned plugin routes — MCP connect without a yaml file."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from ..auth.models import TokenPayload
from ..dependencies import require_auth
from .mcp_manifest import (
    AUTH_VAR_PATTERN,
    agent_did_from_name,
    build_mcp_emerge_yaml,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])


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
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Any:
    try:
        yaml_text = build_mcp_emerge_yaml(
            name=body.name,
            transport=body.transport,
            endpoint=body.endpoint,
            command=body.command,
            args=body.args,
            auth_var=body.auth_var if body.auth_value else None,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # The manifest pins the DID from the name, so the vault key is known
    # before the Registry answers. Store the credential FIRST: if the vault
    # refuses, nothing is registered and the caller can simply retry. The
    # old order (register, then vault) returned 201 on a failed vault write
    # and left a registered MCP with no credential behind it.
    agent_id = agent_did_from_name(body.name)
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
    resp = await request.app.state.registry.post(
        "/api/v1/agents/register", files=files, headers=headers
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    registered = (data.get("data") or {}).get("agent_id")
    if registered and registered != agent_id:
        logger.warning(
            "mcp connect: registry returned %s for manifest id %s", registered, agent_id
        )
    return data
