"""Dev-mode agent registration proxy — forwards to Registry service."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from ..auth.models import TokenPayload
from ..dependencies import require_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dev/agents", tags=["dev-agents"])


async def _proxy(request: Request, method: str, path: str, **kwargs: Any) -> Response:
    registry = request.app.state.registry
    # Forward the caller's JWT so the registry can extract the real user_id
    # even when DISABLE_AUTH=true (our verify_token decodes it without verification).
    fwd_headers = kwargs.pop("headers", {})
    auth = request.headers.get("authorization")
    if auth:
        fwd_headers = {**fwd_headers, "authorization": auth}
    resp = await registry.request(method, path, headers=fwd_headers, **kwargs)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.get("")
async def list_agents(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    return await _proxy(request, "GET", "/api/v1/agents")


@router.post("", status_code=201)
async def register_agent(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    return await _proxy(
        request,
        "POST",
        "/api/v1/agents/register",
        content=body,
        headers={"content-type": content_type},
    )


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    return await _proxy(request, "GET", f"/api/v1/agents/{agent_id}")


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    body = await request.json()
    return await _proxy(request, "PUT", f"/api/v1/agents/{agent_id}", json=body)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    return await _proxy(request, "DELETE", f"/api/v1/agents/{agent_id}")
