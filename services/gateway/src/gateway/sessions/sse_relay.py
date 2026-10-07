"""SSE relay — passes SuperAgent events through verbatim to the client.

SuperAgent owns the canonical SSE event shape (type-based, not event_class-based).
This relay is intentionally thin: it streams bytes through without transformation,
only logging unknown event types for observability.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from internal_commons.sse.events import is_known_event_type

logger = logging.getLogger(__name__)


async def proxy_superagent_sse(
    superagent_client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
) -> AsyncIterator[str]:
    """
    Proxy an SSE stream from SuperAgent to the client with no transformation.

    Yields SSE-formatted strings: ``data: {...}\\n\\n``
    Yields a ``{"type": "error", ...}`` event on connection failure.
    """
    try:
        async with superagent_client.stream("POST", path, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if not raw:
                    continue

                # Validate JSON and log unknown types — but always forward.
                try:
                    event = json.loads(raw)
                    event_type = event.get("type", "")
                    if event_type and not is_known_event_type(event_type):
                        logger.warning(
                            "SSE relay: unknown event type %r — forwarding anyway",
                            event_type,
                        )
                except json.JSONDecodeError:
                    logger.warning("SSE relay: malformed line: %r", line[:200])
                    continue

                yield f"data: {raw}\n\n"
    except httpx.HTTPStatusError as exc:
        logger.error("SSE relay upstream HTTP error: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'error': f'Upstream error {exc.response.status_code}'})}\n\n"
    except Exception:
        # The detail stays in this log. The browser gets a fixed message: an
        # exception's text can carry internal host names and addresses.
        logger.exception("SSE relay connection error")
        yield f"data: {json.dumps({'type': 'error', 'error': 'Upstream connection error'})}\n\n"
