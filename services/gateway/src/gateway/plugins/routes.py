"""User-owned plugin routes — MCP connect without a yaml file."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.models import TokenPayload
from ..dependencies import require_auth
from .mcp_manifest import agent_did_from_name, build_mcp_emerge_yaml

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])


class ConnectMcpRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    transport: Literal["sse", "stdio"]
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    auth_var: str | None = None
    auth_value: str | None = None


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
    agent_id = (data.get("data") or {}).get("agent_id") or agent_did_from_name(
        body.name
    )
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
                "mcp connect registered but vault write failed: %s", cred.text
            )
    return data
