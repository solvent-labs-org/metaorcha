FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

# Exact versions, from this agent's uv.lock.
RUN pip install --no-cache-dir "orcha-sdk==0.1.3" "pytest==9.1.1"

COPY agents/test-runner-agent/agent.py ./
COPY agents/test-runner-agent/fixtures ./fixtures

# The bed switches TEST_RUNNER_REPO between the two fixtures: `passing` for a
# recorded, settled run; `failing` for a refused one. Nothing else is
# configurable from a task.
ENV TEST_RUNNER_REPO=/app/fixtures/passing \
    TEST_RUNNER_CMD="pytest -q" \
    EMERGE_ADVERTISE_HOST=test-runner

EXPOSE 8903
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8903/health', timeout=5)"

CMD ["python", "agent.py"]
