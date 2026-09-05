"""SuperAgent FastAPI application — lifespan wiring and app factory."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.utils.src.logging_config import setup_logging

from .api.routes import router
from .config import settings

logger = logging.getLogger(__name__)

# Module-level singletons — populated by lifespan, not used externally
_pnd_client: Any = None
_scheduler: Any = None
# AsyncRedisSaver.from_conn_string() is an async context manager — keep it open for app lifetime
_redis_checkpointer_cm: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Application lifespan — startup and graceful shutdown."""
    global _pnd_client, _scheduler, _redis_checkpointer_cm

    setup_logging("superagent", settings.log_level)

    # Expose DATABASE_URL to os.environ so Prisma() clients (which read it
    # directly from the process env) work without a datasource override.
    os.environ.setdefault("DATABASE_URL", settings.database_url)

    # Pydantic loads .env into Settings but not os.environ; platform manifests and
    # MCP stdio children expect these names in the process environment.
    if settings.tavily_api_key:
        os.environ.setdefault("TAVILY_API_KEY", settings.tavily_api_key)
    if settings.firecrawl_api_key:
        os.environ.setdefault("FIRECRAWL_API_KEY", settings.firecrawl_api_key)

    logger.info("Starting SuperAgent service")

    # KYA gate config guard (2.1 review): the gate flag without envelope
    # production is a silent free tier — refuse to boot rather than degrade.
    from .pricing.settle_gate import validate_gate_config

    validate_gate_config(settings)

    # 1. Seed platform system MCP tools into Registry, then build boot-time baseline cache
    from .clients.registry_client import RegistryClient
    from .startup.platform_mcp_baseline import load_baseline_from_manifests
    from .startup.platform_tool_seeder import PlatformToolSeeder

    _registry_client = RegistryClient(
        settings.registry_service_url, settings.registry_internal_key
    )
    await PlatformToolSeeder(_registry_client, settings.emerge_tools_dir).seed()
    load_baseline_from_manifests(settings.emerge_tools_dir)

    # 2. Register all system tools (loads sentence-transformers lazily)
    from .system_tools.registry import register_all_system_tools

    register_all_system_tools()
    logger.info("System tools registered")

    # Warm up PnD gate encoder in a background thread so Tier-2 never cold-starts
    # during a live request (all-MiniLM-L6-v2 is ~80 MB, takes ~2–5 s on first load).
    import asyncio as _asyncio

    from .pnd.gate import warm_up_gate_encoder

    loop = _asyncio.get_event_loop()
    loop.run_in_executor(None, warm_up_gate_encoder)
    logger.info("PnD gate encoder warm-up started in background thread")

    # 2. PnD client
    from .pnd.client import PnDClient

    _pnd_client = PnDClient()
    await _pnd_client.start()
    # Expose to orchestrator node (avoids circular imports)
    from .nodes import _registry as _node_registry

    _node_registry["pnd_client"] = _pnd_client
    logger.info("PnD client started → %s", settings.pnd_service_url)

    # 3. LangGraph checkpointer + graph
    # AsyncRedisSaver.from_conn_string returns an async context manager, not the saver itself.
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        cm = AsyncRedisSaver.from_conn_string(settings.redis_url)
        checkpointer = await cm.__aenter__()
        _redis_checkpointer_cm = cm
        await checkpointer.asetup()
        logger.info("RedisSaver checkpointer initialised")
    except Exception as exc:
        redis_target = (
            settings.redis_url.split("@")[-1]
            if "@" in settings.redis_url
            else settings.redis_url
        )
        err = str(exc)
        if "FT._LIST" in err and "unknown command" in err:
            logger.warning(
                "Redis does not expose RediSearch (%s). "
                "LangGraph Redis checkpointer requires Redis Stack. "
                "Falling back to MemorySaver (non-persistent).",
                redis_target,
            )
        else:
            logger.exception(
                "Redis checkpointer failed (%s) — falling back to MemorySaver (non-persistent)",
                redis_target,
            )
        if _redis_checkpointer_cm is not None:
            try:
                await _redis_checkpointer_cm.__aexit__(*sys.exc_info())
            except Exception:
                logger.exception("Redis checkpointer context exit after failed setup")
            _redis_checkpointer_cm = None
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()

    from .graph.builder import build_superagent_graph

    graph = build_superagent_graph(checkpointer_override=checkpointer)

    from .graph.runner import SessionRunner

    app.state.runner = SessionRunner(graph)
    logger.info("LangGraph graph compiled and runner initialised")

    # 4. Workflow scheduler
    from .workflow.scheduler import WorkflowScheduler

    _scheduler = WorkflowScheduler()
    await _scheduler.start()

    # 5. Execution observers (KYA) — each opt-in via its own feature flag.
    # set_observer holds exactly ONE observer, so installing each enabled
    # observer directly would be last-wins (e.g. RUN_ATTESTATION_ENABLED
    # overwriting CDVObserver would silently disable CDV scoring and leave
    # steps[].cdv_bp permanently empty — only CDVObserver writes
    # record.metadata["cdv"]). Collect them and install a single
    # CompositeObserver instead. Order matters: CDVObserver must precede
    # RunAttestationObserver so metadata["cdv"] is populated before the
    # attestation observer accumulates the step.
    from .middleware.observers import (
        CompositeObserver,
        ExecutionObserver,
        set_observer,
    )

    observers: list[ExecutionObserver] = []

    # Audit ledger observer (KY-A, WS7) — opt-in via AUDIT_LEDGER_ENABLED.
    # Stock OSS keeps the NoOpObserver; the ledger observer fails closed and
    # never affects the user-facing execution path.
    if settings.audit_ledger_enabled:
        from .middleware.audit_ledger import LedgerObserver

        observers.append(LedgerObserver())
        logger.info("Audit ledger observer enabled (AUDIT_LEDGER_ENABLED=true)")

    if settings.cdv_verification_enabled:
        from .verification.cdv_integration import build_cdv_observer

        observers.append(build_cdv_observer())

    # Run attestation observer (KYA, RFC 0003) — opt-in via
    # RUN_ATTESTATION_ENABLED. Default off keeps stock OSS behaviour. The
    # validator package is an optional workspace member; degrade gracefully
    # (warning, stock observer) when it is not installed — same contract as
    # sign_case_attestation.
    if settings.run_attestation_enabled:
        try:
            from validator.run_observer import RunAttestationObserver
        except ImportError:
            if settings.settlement_require_attestation:
                # Graceful degrade is fine for attestation alone, but with the
                # gate flag on it becomes the silent free tier the boot guard
                # exists to prevent — same failure, different cause (2.1 review).
                raise RuntimeError(
                    "SETTLEMENT_REQUIRE_ATTESTATION=true but the validator "
                    "package is unavailable — no envelopes can be produced, so "
                    "every charged call would defer forever and never be "
                    "billed. Install the validator package or disable the gate."
                ) from None
            logger.warning(
                "RUN_ATTESTATION_ENABLED=true but the validator package is "
                "unavailable — run attestation disabled"
            )
        else:
            attestation_observer = RunAttestationObserver(
                charter_hash=settings.run_attestation_charter_hash
            )
            observers.append(attestation_observer)
            logger.info(
                "Run attestation observer enabled (RUN_ATTESTATION_ENABLED=true, "
                "charter binding=%s)",
                "active" if settings.run_attestation_charter_hash else "inactive",
            )
            # Attestation-gated settle (AD-1/AD-2) — the gate observer must fire
            # AFTER the attestation observer seals the run (composite dispatch
            # is in registration order), so it resolves the just-sealed run_id.
            if settings.settlement_require_attestation:
                from .pricing.settle_gate import SettlementGateObserver

                observers.append(SettlementGateObserver(attestation_observer))
                logger.info(
                    "Settlement gate observer enabled "
                    "(SETTLEMENT_REQUIRE_ATTESTATION=true)"
                )

    # (The SETTLEMENT_REQUIRE_ATTESTATION-without-RUN_ATTESTATION_ENABLED combo
    # hard-fails at lifespan start via validate_gate_config — no warning here.)

    if len(observers) == 1:
        set_observer(observers[0])
    elif observers:
        set_observer(CompositeObserver(observers))
        logger.info(
            "Composite observer installed: %s",
            ", ".join(type(o).__name__ for o in observers),
        )

    logger.info("SuperAgent ready on port %d", settings.port)

    yield  # ── app is running ──

    # Shutdown
    logger.info("Shutting down SuperAgent…")
    if _redis_checkpointer_cm is not None:
        try:
            await _redis_checkpointer_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("Redis checkpointer context exit on shutdown")
        _redis_checkpointer_cm = None
    if _scheduler:
        await _scheduler.stop()
    if _pnd_client:
        await _pnd_client.stop()
    from .middleware.manifest_cache import MANIFEST_CACHE

    await MANIFEST_CACHE.close()
    logger.info("SuperAgent shutdown complete")


app = FastAPI(
    title="Orcha SuperAgent",
    description=(
        "LangGraph ReAct orchestration runtime — dynamically selects and invokes "
        "MCP/A2A agents at conversation time."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "superagent", "status": "running", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "superagent.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
