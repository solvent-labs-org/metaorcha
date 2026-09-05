"""Attestation playground FastAPI app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .events import StepEventRouter
from .modes import probe_superagent
from .real_runner import RealModeError, create_session, run_real_turn
from .sim_runner import run_sim
from .state import run_store
from .verify import load_golden, verify_envelope

logger = logging.getLogger(__name__)

DEFAULT_SUPERAGENT_URL = "http://localhost:8002"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/orcha"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.superagent_url = os.environ.get("SUPERAGENT_URL", DEFAULT_SUPERAGENT_URL)
    app.state.database_url = os.environ.get(
        "PLAYGROUND_DATABASE_URL", DEFAULT_DATABASE_URL
    )
    app.state.step_delay = 0.6
    app.state.real_available = await probe_superagent(app.state.superagent_url)
    configured = os.environ.get("PLAYGROUND_MODE", "auto")
    if configured == "sim" or (configured == "auto" and not app.state.real_available):
        app.state.mode = "sim"
    else:
        app.state.mode = "real"
    app.state.step_router = None
    app.state.kafka_active = False
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    if app.state.mode == "real" and bootstrap:
        try:
            router = StepEventRouter(bootstrap)
            await router.start()
            app.state.step_router = router
            app.state.kafka_active = True
        except Exception:
            logger.exception(
                "Kafka consumer start failed; continuing without live steps"
            )
    logger.info(
        "playground mode=%s real_available=%s", app.state.mode, app.state.real_available
    )
    yield
    router = app.state.step_router
    stop = getattr(router, "stop", None)
    if stop is not None:
        try:
            await stop()
        except Exception:
            logger.exception("Kafka consumer stop failed")


app = FastAPI(title="Orcha Attestation Playground", lifespan=lifespan)
app.state.step_delay = 0.6  # tests override to 0.0
app.state.mode = "sim"  # defaults when lifespan has not run (plain TestClient)
app.state.real_available = False
app.state.kafka_active = False


def sse_line(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/mode")
async def mode() -> dict[str, Any]:
    return {
        "mode": app.state.mode,
        "superagent_url": app.state.superagent_url
        if app.state.mode == "real"
        else None,
        "kafka": app.state.kafka_active,
    }


@app.post("/verify")
async def verify(body: Any = Body(...)) -> dict[str, Any]:
    return verify_envelope(body)


@app.get("/golden")
async def golden() -> dict[str, Any]:
    data = load_golden()
    return {"valid": data["valid"], "tampered": data["tampered"]}


@app.post("/runs")
async def create_run(body: dict[str, Any] = Body(...)) -> dict[str, str]:
    prompt = body.get("prompt") or "demo run"
    requested = body.get("mode") or app.state.mode
    if requested == "real":
        if not app.state.real_available:
            raise HTTPException(
                status_code=409,
                detail="superagent unreachable; start the harness stack or retry with mode=sim",
            )
        try:
            session_id = await create_session(app.state.superagent_url, prompt)
        except RealModeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        run_store.create(session_id, "real")
        router = app.state.step_router
        if router is not None:
            router.register(session_id, run_store.sink(session_id))
        asyncio.create_task(
            run_real_turn(
                session_id,
                prompt,
                base_url=app.state.superagent_url,
                database_url=app.state.database_url,
                step_delay=app.state.step_delay,
            )
        )
        return {"run_id": session_id, "mode": "real"}
    run_id = f"sim-{uuid.uuid4().hex[:12]}"
    run_store.create(run_id, "sim")
    asyncio.create_task(run_sim(run_id, prompt, step_delay=app.state.step_delay))
    return {"run_id": run_id, "mode": "sim"}


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    if run_store.get(run_id) is None:
        raise HTTPException(status_code=404, detail="unknown run_id")

    async def gen() -> AsyncIterator[str]:
        history, queue = run_store.subscribe(run_id)
        try:
            for event in history:
                yield sse_line(event)
                if event.get("type") in {"envelope_ready", "run_failed"}:
                    return
            while True:
                event = await queue.get()
                yield sse_line(event)
                if event.get("type") in {"envelope_ready", "run_failed"}:
                    return
        finally:
            run_store.unsubscribe(run_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/runs/{run_id}/envelope")
async def run_envelope(run_id: str) -> dict[str, Any]:
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    if run["envelope"] is None:
        raise HTTPException(status_code=409, detail="envelope not ready")
    return run["envelope"]


STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
