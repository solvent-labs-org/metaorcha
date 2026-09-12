"""Gateway FastAPI application."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.utils.src.logging_config import setup_logging

from .config import settings

logger = logging.getLogger(__name__)


# Export Privy env vars from settings so privy_client.py can read them at call time
# (privy_client uses os.environ directly to avoid circular imports).
def _export_privy_env() -> None:
    """Push Privy credentials from settings into os.environ for privy_client.py."""
    if settings.privy_app_id:
        os.environ.setdefault("PRIVY_APP_ID", settings.privy_app_id)
    if settings.privy_app_secret:
        os.environ.setdefault("PRIVY_APP_SECRET", settings.privy_app_secret)
    if settings.privy_webhook_secret:
        os.environ.setdefault("PRIVY_WEBHOOK_SECRET", settings.privy_webhook_secret)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging("gateway", settings.log_level, extra_namespaces=["apscheduler"])

    # Push Privy credentials into env so privy_client.py can read them.
    _export_privy_env()

    # Prisma — uses the shared common-database generated client.
    # Pass DATABASE_URL as a datasource override so the query engine subprocess
    # picks it up even when DATABASE_URL isn't set as an OS env var.
    from common.database.src.generated_client import Prisma

    db = Prisma(datasource={"url": settings.database_url})
    await db.connect()
    app.state.db = db

    # Redis (DB index is embedded in REDIS_URL, e.g. redis://…/1)
    app.state.redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    # httpx clients for downstream services
    app.state.superagent = httpx.AsyncClient(
        base_url=settings.superagent_url,
        timeout=httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0),
        http2=True,
    )
    app.state.registry = httpx.AsyncClient(
        base_url=settings.registry_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )

    # APScheduler — only started in testnet / mainnet (real on-chain settlement).
    # mock mode skips the scheduler entirely — credits are synthetic.
    settlement_chain = "base" if settings.payment_mode == "mainnet" else "base_sepolia"
    scheduler = None
    if settings.payment_mode not in ("testnet", "mainnet"):
        logger.info(
            "APScheduler skipped — payment_mode=%s (scheduler only runs in testnet/mainnet)",
            settings.payment_mode,
        )
    else:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            from .jobs.balance_sync import run_wallet_balance_sync
            from .jobs.metrics_refresh import run_metrics_refresh
            from .jobs.settlement import run_settlement

            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                run_settlement,
                "interval",
                seconds=settings.settlement_interval_seconds,
                args=[
                    db,
                    app.state.redis,
                    settings.gateway_instance_id,
                    settlement_chain,
                ],
                id="settlement",
                replace_existing=True,
            )
            # Polls each user's smart wallet USDC balance on-chain and credits
            # any delta — patches the missing Privy webhook for AA smart wallets.
            scheduler.add_job(
                run_wallet_balance_sync,
                "interval",
                seconds=settings.wallet_balance_sync_interval_seconds,
                args=[
                    db,
                    app.state.redis,
                    settlement_chain,
                    settings.base_sepolia_rpc_url,
                ],
                id="wallet_balance_sync",
                replace_existing=True,
            )
            scheduler.add_job(
                run_metrics_refresh,
                "interval",
                seconds=settings.metrics_refresh_interval_seconds,
                args=[db],
                id="metrics_refresh",
                replace_existing=True,
            )
            scheduler.start()
            logger.info(
                "APScheduler started — settlement + wallet_balance_sync + metrics_refresh"
                " (chain=%s payment_mode=%s)",
                settlement_chain,
                settings.payment_mode,
            )
        except ImportError:
            logger.warning("apscheduler not installed — cron jobs disabled")

    app.state.scheduler = scheduler

    logger.info(
        "Gateway ready — port=%d superagent=%s registry=%s payment_mode=%s",
        settings.port,
        settings.superagent_url,
        settings.registry_url,
        settings.payment_mode,
    )
    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shutdown")

    await app.state.superagent.aclose()
    await app.state.registry.aclose()
    await app.state.redis.aclose()
    await db.disconnect()
    logger.info("Gateway shutdown complete")


app = FastAPI(
    title="Orcha Gateway",
    version="0.1.0",
    description="Public ingress — JWT auth, SSE relay, session management",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.getenv("SANDBOX_MODE", "").lower() == "true":
    from .sandbox_guard import SandboxGuardMiddleware

    app.add_middleware(SandboxGuardMiddleware)
    logger.info("SandboxGuardMiddleware active (SANDBOX_MODE=true)")

# Routers — imported after `app` to avoid circular imports  # noqa: E402
from .agents.routes import router as agents_router  # noqa: E402
from .auth.routes import router as auth_router  # noqa: E402
from .credentials.routes import router as credentials_router  # noqa: E402
from .files.routes import router as files_router  # noqa: E402
from .health import router as health_router  # noqa: E402
from .runs.routes import router as runs_router  # noqa: E402
from .sessions.routes import router as sessions_router  # noqa: E402
from .settings_router.routes import router as settings_router  # noqa: E402
from .wallet.routes import router as wallet_router  # noqa: E402
from .wallet.webhook import router as wallet_webhook_router  # noqa: E402
from .workflows.routes import router as workflows_router  # noqa: E402

app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(runs_router)
app.include_router(files_router)
app.include_router(credentials_router)
app.include_router(workflows_router)
app.include_router(agents_router)
app.include_router(settings_router)
app.include_router(wallet_router)
app.include_router(wallet_webhook_router)
app.include_router(health_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "orcha-gateway", "version": "0.1.0"}
