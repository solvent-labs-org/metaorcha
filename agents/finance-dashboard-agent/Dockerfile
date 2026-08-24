FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Exact versions, taken from this agent's uv.lock. Floors (>=) let the image
# drift to whatever resolved on build day, so two builds of the same commit
# could ship different dependency trees.
COPY agents/finance-dashboard-agent/pyproject.toml agents/finance-dashboard-agent/uv.lock ./
RUN pip install --no-cache-dir \
    "mcp[cli]==1.29.0" \
    "fastmcp==3.4.5" \
    "starlette==1.3.1" \
    "uvicorn[standard]==0.52.1" \
    "python-dotenv==1.2.2"

COPY agents/finance-dashboard-agent/server.py ./

ENV PYTHONPATH="/app"

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:3010/health || exit 1

EXPOSE 3010

CMD ["python", "server.py"]
