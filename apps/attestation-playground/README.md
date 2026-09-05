# Attestation Playground

Local demo for RFC 0003 run attestation (`docs/spec/rfcs/0003-run-attestation-envelope.md`):
run an agent, watch the step hash chain build live, download the signed
envelope, then tamper with it and watch offline verification fail.

## Quickstart (sim mode — zero dependencies)

```bash
make install  # or: uv sync --all-packages  (from repo root, once)
cd apps/attestation-playground
uv run uvicorn playground.main:app --port 8040
```

Open http://localhost:8040. Click **Run agent** → the chain builds →
**Copy → verifier** → flip any byte in the JSON → **Verify** → red.

No envelope verifies because the playground says so — verification is the
same vendored code the `emerge verify` CLI uses. The download button gives
you the envelope file; `emerge verify <file>` agrees with the browser.

Refresh the browser mid-run: the feed replays from the server's event log
and continues live — nothing is lost.

## Real mode (against the local harness stack)

```bash
./scripts/run-all.sh   # infra + services + seed agents, from repo root
RUN_ATTESTATION_ENABLED=true  # must be set on the superagent (its .env)
cd apps/attestation-playground
uv run uvicorn playground.main:app --port 8040
```

At startup the playground probes `SUPERAGENT_URL` (default
`http://localhost:8002`); if healthy it runs in real mode, else sim.
Real runs create a superagent session, stream live step events from the
`execution.step_complete` Kafka topic, and read the persisted envelope from
Postgres (read-only). The mode badge in the header always shows which mode
you are in.

Env vars: `PLAYGROUND_MODE` (`auto`|`real`|`sim`), `SUPERAGENT_URL`,
`PLAYGROUND_DATABASE_URL`, `KAFKA_BOOTSTRAP_SERVERS`.

## Demo script (for a talk)

1. **Golden open** — click "Golden: valid" → green. "This is the RFC 0003
   test vector; verification needs nothing from us — no API, no network."
2. **Sim run** — Run agent (auto/sim) → chain grows step by step → envelope
   appears, signed by `did:orcha:system:playground-demo`.
3. **The break** — Copy → verifier, change one digit in `latency_ms`,
   Verify → red: `step hash chain does not match steps_root`.
4. **Real run** (stack up) — same flow against live agents; live Kafka steps
   stream in first, then the signed chain reveal.
5. **CLI parity** — Download the envelope, run `emerge verify <file>` in a
   terminal → same verdict, exit code 0. Flip a byte → exit code 1.

## Tests

```bash
cd apps/attestation-playground && uv run pytest tests/ -v
```

Tests need no Kafka, Postgres, or running services (all infra mocked).
