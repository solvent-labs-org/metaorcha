"""SuperAgent settings — fail-early at module import if env vars are missing."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class Settings(BaseSettings):
    # LLM / OpenRouter
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Orchestration LLM (OpenRouter id). Override with ORCHESTRATOR_MODEL — prefer a small/cheap
    # model here (e.g. Haiku, Gemini Flash); keep sonnet only if you need heavier reasoning.
    orchestrator_model: str = "anthropic/claude-haiku-4.5"
    # PnD gate Tier-3 classifier — cheap model is fine (same env pattern as orchestrator).
    small_model: str = "anthropic/claude-haiku-4.5"

    # Infrastructure
    redis_url: str
    database_url: str
    vault_key: str  # base64-encoded 32-byte key for AES-256-GCM

    # Dependent services
    pnd_service_url: str
    registry_service_url: str = "http://localhost:8000"
    registry_internal_key: str = "dev-internal-secret"

    # Platform MCP secrets (optional — read from .env; copied to os.environ at boot for
    # PlatformToolSeeder checks and stdio MCP subprocess env)
    tavily_api_key: str | None = None
    firecrawl_api_key: str | None = None

    # Platform tool manifests (common/emerge-tools — manifests + servers/Chomper)
    # Overridable via EMERGE_TOOLS_DIR — container default matches Dockerfile COPY
    emerge_tools_dir: str = "/app/common/emerge-tools"

    # S3 / Artifact storage (boto3 — LocalStack in dev, real AWS in prod)
    artifact_s3_bucket: str = "emerge-artifacts-local"
    s3_endpoint_url: str | None = (
        None  # None = real AWS; set to http://localstack:4566 for dev
    )
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"

    # Server
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8002
    log_level: str = "info"

    # Context window
    token_budget: int = 140_000
    compression_threshold: float = 0.85  # compress when usage > 85% of budget
    keep_last_messages: int = 15

    # Verifier retry-gate (v1.3) — bounded re-runs of a step that fails with a
    # transient error before the result is committed. 0 disables retries.
    verify_max_retries: int = 2

    # Max completion tokens per orchestrator LLM call. 1024 is the stock OSS
    # default; multi-step playbooks (e.g. KY-A supervision) need headroom so a
    # completion is not truncated before its tool call.
    orchestrator_max_tokens: int = 1024

    # Audit ledger (KY-A, WS7) — when enabled, a LedgerObserver is installed at
    # boot and every completed step is appended to the hash-chained audit_ledger
    # table. Default off keeps stock OSS behaviour (NoOpObserver).
    audit_ledger_enabled: bool = False

    # KY-A supervisor cyber guardrails (WS10, FR-10.1) — when enabled, the run
    # is scope-limited to the allowlisted agent DIDs below plus an explicit set
    # of system tools (see kya_policy.py). Default off keeps stock OSS behaviour.
    kya_mode_enabled: bool = False
    kya_allowed_agents: str = (
        "did:orcha:agent:kya-verification,"
        "did:orcha:agent:rulebook-rag,"
        "did:orcha:agent:payment-anomaly"
    )

    # DAG planner routing (Slice 1) — when enabled, complex goals are routed to
    # the PnD 5-stage DAG planner (/api/v1/plan) and executed as a planned
    # workflow; simple goals stay on the ReAct loop. Default off keeps stock
    # OSS behaviour (pure ReAct).
    dag_planner_enabled: bool = False
    dag_route_high: float = 0.75  # Channel A score >= high → DAG
    dag_route_low: float = 0.35  # Channel A score <= low → ReAct; between → Channel B
    dag_route_model: str | None = None  # Channel B arbiter; None → small_model

    # CDV verification (Slice 1) — per-step Channel-A scoring observer +
    # AdaptiveStopper loop backstop. Default off keeps stock OSS behaviour.
    cdv_verification_enabled: bool = False
    cdv_store_dir: str = ".cdv-runs"  # per-run SQLite: {dir}/{session_id}.db

    # Run attestation (KYA, RFC 0003) — when enabled, a RunAttestationObserver
    # is installed at boot and every completed run is sealed into a signed
    # orcha.run-attestation/v1 envelope (persisted via the Attestation model).
    # Requires the optional validator package; degrades to a warning without it.
    # Default off keeps stock OSS behaviour (NoOpObserver).
    run_attestation_enabled: bool = False

    # RUN_ATTESTATION_CHARTER_HASH — when set, produced envelopes carry this
    # charter_hash: the run is on the settling path (AD-9 static fixture
    # expectation — for the slice demo this is the sha256 of the canonical
    # demo-charter fixture, docs/spec/fixtures/demo-charter.json). When unset,
    # envelopes carry charter_hash: null (non-settling path). Presence of this
    # setting IS the settling-path toggle for the MVP; Epic 3's dual-run
    # script drives the two legs via it. Note: settings validation runs at
    # every boot regardless of run_attestation_enabled — a genuinely malformed
    # value fails loud at startup (by design); empty/blank is treated as unset.
    run_attestation_charter_hash: str | None = None

    @field_validator("run_attestation_charter_hash")
    @classmethod
    def _charter_hash_hex64(cls, value: str | None) -> str | None:
        # Re-declared locally — never import from the optional validator
        # package at settings load (graceful-degrade contract). A mirrored copy
        # of this rule lives in validator/run_envelope.py (_HEX64_RE) — keep
        # the two in sync.
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            # VAR= (empty/blank) is the standard "unset" idiom — behave as unset.
            return None
        if not _HEX64_RE.fullmatch(stripped):
            raise ValueError(
                "run_attestation_charter_hash must be None or 64 lowercase hex chars"
            )
        return stripped

    # SETTLEMENT_REQUIRE_ATTESTATION (AD-2) — when true, mock settlement
    # requires a valid run attestation for the run_id: per-call step-6.5
    # settle defers, and a gate observer settles post-seal only after the
    # vendored verifier passes (fail-closed on any missing/invalid proof).
    # Default off keeps stock OSS settle behaviour. PAYMENT_MODE enum is
    # unchanged by this flag.
    settlement_require_attestation: bool = False

    # Bound a single agent step's text output before it re-enters the
    # orchestrator context. Protects rate-limited tiers (per-minute token
    # ceilings) from unbounded scrape/crawl outputs. 0 disables truncation.
    tool_output_max_chars: int = 4000

    # Hard per-agent-call ceiling (seconds). A hung agent becomes an Error
    # string (retryable, classified) instead of a silent stall.
    agent_call_timeout_seconds: int = 60

    # Fleet filter: comma-separated agent DIDs excluded from discovery
    # (e.g. sandbox deployments hiding credential-requiring demo agents).
    agent_exclude_ids: str = ""

    # Sandbox demo mailer — registers the send_run_receipt system tool
    # (emails a run's audit via Resend). Off by default.
    sandbox_mailer: bool = False

    @property
    def agent_exclude_id_list(self) -> list[str]:
        return [a.strip() for a in self.agent_exclude_ids.split(",") if a.strip()]

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


# Raises pydantic.ValidationError at import time if required vars are missing
settings = Settings()
