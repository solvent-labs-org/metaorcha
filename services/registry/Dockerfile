# Multi-stage Dockerfile for Registry Service
# Build optimized for production use

# ============================================================================
# Stage 1: Base image with uv and Python
# ============================================================================
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# ============================================================================
# Stage 2: Dependencies
# ============================================================================
FROM base AS dependencies

# Copy workspace configuration
COPY pyproject.toml uv.lock ./

# Copy common packages
COPY common/database ./common/database
COPY common/proto ./common/proto
COPY common/utils ./common/utils
COPY common/kafka ./common/kafka

# Copy service package
COPY services/registry/pyproject.toml ./services/registry/

# Install dependencies
RUN uv sync --frozen --no-dev --package registry-service

# ============================================================================
# Stage 3: Builder (Generate Prisma client and gRPC stubs)
# ============================================================================
FROM dependencies AS builder

# Official Node.js tarball (npm 10) — Debian's npm 9 times out fetching large
# package metadata on some docker bridge networks.
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && curl -fsSL https://nodejs.org/dist/v20.20.2/node-v20.20.2-linux-x64.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && rm -rf /var/lib/apt/lists/*

# Generate Prisma client
WORKDIR /app/common/database
ENV HOME=/app \
    PATH="/app/.venv/bin:$PATH"
ENV NODE_OPTIONS=--dns-result-order=ipv4first
RUN mkdir -p /app/.cache/prisma-python && \
    prisma generate && prisma py fetch

# Generate gRPC stubs
WORKDIR /app/common/proto
RUN uv pip install --python /app/.venv/bin/python --no-cache-dir grpcio-tools && \
    /app/.venv/bin/python -m grpc_tools.protoc \
    -I./src \
    --python_out=./src \
    --grpc_python_out=./src \
    ./src/registry.proto

WORKDIR /app

# ============================================================================
# Stage 4: Runtime
# ============================================================================
FROM base AS runtime

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/.cache && chown -R appuser:appuser /app

# Copy installed dependencies from builder
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/common /app/common
COPY --from=builder --chown=appuser:appuser /app/.cache /app/.cache

# Copy service source code
COPY --chown=appuser:appuser services/registry/src ./services/registry/src
COPY --chown=appuser:appuser services/registry/.env.example ./services/registry/.env.example

# Switch to non-root user
USER appuser

# Set Python path
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:/app/common/proto/src:$PYTHONPATH" \
    HOME=/app

# Expose ports
EXPOSE 8000 50051

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/v1/health', timeout=5)"

# Start the service
CMD ["python", "-m", "uvicorn", "services.registry.src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
