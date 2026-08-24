# Multi-stage Dockerfile for Gateway Service

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
COPY common/utils ./common/utils
COPY common/internal-commons ./common/internal-commons

COPY services/gateway/pyproject.toml ./services/gateway/

RUN uv sync --frozen --no-dev --package gateway && \
    uv pip install --python /app/.venv/bin/python --no-cache-dir python-multipart

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

RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/.cache && chown -R appuser:appuser /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/common /app/common
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache

COPY --chown=appuser:appuser services/gateway/src ./services/gateway/src
COPY --chown=appuser:appuser services/gateway/.env.example ./services/gateway/.env.example

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:$PYTHONPATH" \
    HOME=/app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=5)"

CMD ["python", "-m", "uvicorn", "gateway.main:app", \
     "--app-dir", "services/gateway/src", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1"]
