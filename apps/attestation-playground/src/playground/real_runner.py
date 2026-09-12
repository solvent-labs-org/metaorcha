"""Real mode: drive a run through the local superagent and fetch its envelope.

Pipeline: POST /sessions -> POST /sessions/{id}/message (SSE to EOF) ->
read-only Postgres query for the persisted attestation row -> chain reveal
from the envelope's canonical steps (same reveal code as sim mode — no
crypto is reimplemented here).

Envelope retrieval still has a read-only Postgres fallback. SuperAgent now
serves the same bytes at ``GET /runs/{run_id}/attestation`` and
``GET /sessions/{session_id}/attestation`` (ownership-checked, not verified).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
import httpx

from .sim_runner import chain_events
from .state import run_store


class RealModeError(RuntimeError):
    """A real-mode dependency (superagent, Postgres) failed."""


async def create_session(base_url: str, prompt: str) -> str:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            resp = await client.post(
                "/sessions",
                json={"user_id": "playground", "title": prompt[:80]},
            )
            resp.raise_for_status()
            return resp.json()["session_id"]
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        raise RealModeError(f"failed to create superagent session: {exc}") from exc


async def run_message_turn(base_url: str, session_id: str, prompt: str) -> None:
    """POST the goal and consume the turn SSE stream until EOF (= turn done)."""
    try:
        timeout = httpx.Timeout(600.0, connect=5.0)
        async with (
            httpx.AsyncClient(base_url=base_url, timeout=timeout) as client,
            client.stream(
                "POST",
                f"/sessions/{session_id}/message",
                json={"user_id": "playground", "message": prompt},
            ) as resp,
        ):
            resp.raise_for_status()
            async for _line in resp.aiter_lines():
                pass  # step events arrive via Kafka; EOF signals completion
    except httpx.HTTPError as exc:
        raise RealModeError(f"superagent turn failed: {exc}") from exc


async def fetch_envelope(
    database_url: str,
    session_id: str,
    *,
    attempts: int = 30,
    interval: float = 0.5,
) -> dict[str, Any]:
    """Poll Postgres for the persisted attestation envelope (read-only)."""
    last_error: Exception | None = None
    for _ in range(attempts):
        row = None
        try:
            conn = await asyncpg.connect(database_url)
            try:
                try:
                    row = await conn.fetchrow(
                        "SELECT payload FROM attestations "
                        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1",
                        session_id,
                    )
                except asyncpg.exceptions.UndefinedColumnError:
                    row = await conn.fetchrow(
                        "SELECT payload FROM attestations WHERE session_id = $1",
                        session_id,
                    )
            finally:
                await conn.close()
        except (OSError, asyncpg.PostgresError) as exc:
            last_error = exc
        if row is not None:
            payload = row["payload"]
            return json.loads(payload) if isinstance(payload, str) else dict(payload)
        await asyncio.sleep(interval)
    raise RealModeError(
        f"no attestation row for session {session_id} after {attempts} attempts"
        + (f" (last error: {last_error})" if last_error else "")
    )


async def run_real_turn(
    run_id: str,
    prompt: str,
    *,
    base_url: str,
    database_url: str,
    step_delay: float = 0.3,
) -> None:
    """Full real-mode lifecycle. run_id IS the superagent session_id."""
    try:
        await run_message_turn(base_url, run_id, prompt)
        envelope = await fetch_envelope(database_url, run_id)
        for event in chain_events(envelope):
            run_store.emit(run_id, event)
            await asyncio.sleep(step_delay)
        run_store.set_envelope(run_id, envelope)
        run_store.emit(run_id, {"type": "envelope_ready", "run_id": run_id})
    except Exception as exc:  # demo must fail visibly, never hang
        run_store.set_failed(run_id, str(exc))
        run_store.emit(run_id, {"type": "run_failed", "error": str(exc)})
