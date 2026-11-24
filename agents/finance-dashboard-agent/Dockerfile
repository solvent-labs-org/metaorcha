FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# The lock is the pin. A version floor, or an exact version with no hash,
# still accepts whatever bytes the index serves for that version on build day.
# --require-hashes rejects a wheel that is not the one uv.lock recorded.
COPY agents/finance-dashboard-agent/pyproject.toml agents/finance-dashboard-agent/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-header -o /tmp/req.txt && \
    uv pip install --system --no-cache --require-hashes -r /tmp/req.txt && \
    rm -f /usr/local/bin/uv /tmp/req.txt

COPY agents/finance-dashboard-agent/server.py ./

ENV PYTHONPATH="/app"

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:3010/health || exit 1

EXPOSE 3010

CMD ["python", "server.py"]
