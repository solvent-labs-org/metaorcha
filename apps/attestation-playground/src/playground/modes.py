"""Startup mode probe: is the real harness stack reachable?"""

from __future__ import annotations

import httpx


async def probe_superagent(base_url: str, *, timeout: float = 2.0) -> bool:
    """True when superagent answers GET /health within the timeout."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
