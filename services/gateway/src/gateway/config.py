"""Gateway configuration — loaded once at startup, fails fast on missing vars."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file (services/gateway/src/gateway/config.py)
# so it works regardless of which directory uvicorn is launched from.
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    environment: str = "development"

    # Infrastructure
    database_url: str
    redis_url: str

    # JWT
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_days: int = 7
    jwt_refresh_token_expire_days: int = 30

    # Downstream services — ports must match the deployment supervisor config:
    # superagent :8001, planning-discovery :8002, registry :8003, gateway :8000
    superagent_url: str = "http://127.0.0.1:8001"
    registry_url: str = "http://127.0.0.1:8003"
    # Internal wallet microservice (Node.js AA executor — not exposed to public internet)
    wallet_service_url: str = "http://localhost:4333/internal"

    # OAuth state signing (HMAC-SHA256)
    # Defaults to jwt_secret_key — override with OAUTH_STATE_SECRET for isolation.
    oauth_state_secret: str = ""

    # Base URL of the deployed gateway (used to build redirect_uri in agent OAuth flow)
    gateway_base_url: str = "http://localhost:8080"

    # Base URL of the frontend (used for post-OAuth redirects)
    ui_base_url: str = "http://localhost:3000"

    # Allowed CORS origins — comma-separated list.
    # allow_origins=["*"] + allow_credentials=True is rejected by browsers;
    # always list explicit origins here.
    cors_origins: str = "http://localhost:3000,https://app.orcha.ai"

    # Privy embedded-wallet credentials (dashboard.privy.io → Settings → API Keys)
    privy_app_id: str = ""
    privy_app_secret: str = ""
    # Webhook verification secret (dashboard.privy.io → Webhooks)
    privy_webhook_secret: str = ""
    # Privy wallet ID of the platform wallet — dispatch wallet (sends all transfers).
    # Users fund this wallet directly. Its on-chain address is shown in the UI.
    platform_wallet_id: str = ""
    # Privy wallet ID of the treasury wallet — receives platform cut.
    # Can be a separate cold wallet. On-chain address auto-resolved from Privy.
    treasury_wallet_id: str = ""

    # Payment mode — controls settlement and fake-credit seeding:
    #   "mock"    — no settlement cron; new users seeded with 5000 USDC credits for testing
    #   "testnet" — settlement cron runs against Base Sepolia USDC
    #   "mainnet" — settlement cron runs against Base mainnet USDC
    payment_mode: Literal["mock", "testnet", "mainnet"] = "mock"

    # Unique identifier for this Gateway instance (used for settlement distributed lock)
    gateway_instance_id: str = "gateway-1"

    # Scheduler intervals (seconds)
    settlement_interval_seconds: int = 300  # 5 minutes
    metrics_refresh_interval_seconds: int = 3600  # 1 hour
    # Smart-wallet USDC balance sync (patches missing Privy webhooks for AA wallets)
    wallet_balance_sync_interval_seconds: int = 10

    # Base Sepolia JSON-RPC endpoint for on-chain balance reads (balance_sync job).
    # Use a private Alchemy/Infura key in production to avoid public rate limits.
    base_sepolia_rpc_url: str = "https://sepolia.base.org"

    # S3 / artifact storage (mirrors SuperAgent settings)
    artifact_s3_bucket: str = "emerge-artifacts-local"
    s3_endpoint_url: str | None = None  # None = real AWS; set for LocalStack
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"

    # CONNECTIONS_ENABLED — user connections (a tool server plus the user's own
    # token, registered from the chat). Off by default: the connect route
    # refuses, and SuperAgent refuses to call a connection (spine AD-18). Must
    # match SuperAgent's setting of the same name.
    connections_enabled: bool = False

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    @property
    def effective_oauth_state_secret(self) -> str:
        """Return the OAuth state secret, falling back to jwt_secret_key."""
        return self.oauth_state_secret or self.jwt_secret_key

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
