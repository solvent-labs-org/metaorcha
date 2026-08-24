# Multi-stage Dockerfile for SuperAgent Service

# ============================================================================
# Stage 1: Base image with uv and Python
# ============================================================================
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# ============================================================================
# Stage 2: Dependencies
# ============================================================================
FROM base AS dependencies

COPY pyproject.toml uv.lock ./

COPY common/database ./common/database
COPY common/kafka ./common/kafka
COPY common/llm ./common/llm
COPY common/pricing ./common/pricing
COPY common/proto ./common/proto
COPY common/utils ./common/utils
COPY common/internal-commons ./common/internal-commons
COPY common/emerge-tools ./common/emerge-tools

COPY services/superagent/pyproject.toml ./services/superagent/

RUN uv sync --frozen --no-dev --package superagent

# ============================================================================
# Stage 3: Builder (Generate Prisma client + fetch engine binary)
# ============================================================================
FROM dependencies AS builder

# Official Node.js tarball (npm 10) — Debian's npm 9 times out fetching large
# package metadata on some docker bridge networks.
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && curl -fsSL https://nodejs.org/dist/v20.20.2/node-v20.20.2-linux-x64.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/common/database
ENV HOME=/app \
    PATH="/app/.venv/bin:$PATH"
ENV NODE_OPTIONS=--dns-result-order=ipv4first
RUN mkdir -p /app/.cache/prisma-python && \
    prisma generate && prisma py fetch

WORKDIR /app

# ============================================================================
# Stage 4: Runtime
# ============================================================================
FROM base AS runtime

# System libs for mcp-document-converter (WeasyPrint → PDF/HTML); Python wheels ship the rest.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/.cache && chown -R appuser:appuser /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/common /app/common
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache

# Chromium headless shell + system deps for the computer-use Playwright backend
# (selected via COMPUTER_USE_BACKEND=playwright; inert under the mock default).
# Fixed browsers path so the appuser runtime finds the root-installed browser.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /app/.venv/bin/playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=appuser:appuser services/superagent/src ./services/superagent/src
COPY --chown=appuser:appuser services/superagent/.env.example ./services/superagent/.env.example

# Platform MCP manifests + servers (e.g. Chomper) for PlatformToolSeeder
COPY --chown=appuser:appuser common/emerge-tools /app/common/emerge-tools

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:$PYTHONPATH" \
    HOME=/app

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8002/health', timeout=5)"

CMD ["python", "-m", "uvicorn", "superagent.main:app", \
     "--app-dir", "services/superagent/src", \
     "--host", "0.0.0.0", \
     "--port", "8002", \
     "--workers", "1"]
