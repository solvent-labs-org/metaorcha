"""Run routes — proxy a sealed RFC 0003 envelope as raw bytes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from ..auth.models import TokenPayload
from ..dependencies import require_auth

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


@router.get("/{run_id}/attestation")
async def get_run_attestation(
    run_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> Response:
    """Proxy SuperAgent ``GET /runs/{run_id}/attestation``.

    JWT becomes the ``user_id`` query param. Body and attestation headers
    pass through untouched — the route does not parse or verify (AR-5).
    """
    sa = request.app.state.superagent
    resp = await sa.get(
        f"/runs/{run_id}/attestation",
        params={"user_id": payload.user_id},
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attestation not found"
        )
    resp.raise_for_status()
    headers: dict[str, str] = {}
    content_disposition = resp.headers.get("content-disposition")
    if content_disposition:
        headers["Content-Disposition"] = content_disposition
    cache_control = resp.headers.get("cache-control")
    if cache_control:
        headers["Cache-Control"] = cache_control
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=headers,
    )
