FROM --platform=linux/amd64 python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv
# Astral's static binary dispatches `uvx` by argv0 — symlink so workspace-mcp spawn works on loopback.
RUN ln -sf /usr/local/bin/uv /usr/local/bin/uvx

WORKDIR /app

# ── Deps layer (cached unless pyproject.toml / uv.lock change) ───────────────
COPY pyproject.toml uv.lock ./
# --no-hashes stripped the integrity check that the lock exists to provide;
# keeping the hashes means a tampered artifact fails the install rather than
# silently entering the image.
RUN uv export --frozen --no-dev --no-emit-project > /tmp/req.txt && \
    uv pip install --system --no-cache --require-hashes -r /tmp/req.txt && \
    uv pip install --system --no-cache "workspace-mcp==1.20.4" && \
    python3 -c "import pydantic_core, fastapi" && \
    command -v workspace-mcp

# ── Source layer (only invalidates when code changes) ─────────────────────────
COPY src/ ./src/

# Default: spawn workspace-mcp on loopback (see server lifespan). WORKSPACE_MCP_URL only if MCP runs elsewhere.
EXPOSE 8080
# Lightsail / platform often inject PORT; default 8080 to match EXPOSE.
CMD ["sh", "-c", "exec uvicorn src.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
