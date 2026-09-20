# Sandbox deploy runbook — Orcha OSS launch

Target: one VM, no managed services, ≈ $0–15/mo. Proven end-to-end locally
2026-07-26 (18 containers healthy, 9 agents seeded, guest flow verified to
the LLM boundary).

## 1. Provision

- VM: 2 vCPU / **8 GB RAM minimum** (Kafka + Ollama + 4 services + agents),
  Ubuntu 24.04, 40 GB disk. AWS credits (EC2 `t3.large`) or Oracle free tier.
- `sudo apt update && sudo apt install -y docker.io docker-compose-v2 git`
- Open ports: 80, 443 only (all service ports stay internal except registry
  8000, which is loopback-only by compose config).

## 2. Deploy

```bash
git clone https://github.com/solvent-labs-org/metaorcha && cd metaorcha
cp deploy/sandbox/.env.sandbox.example deploy/sandbox/.env.sandbox   # fill: POSTGRES_PASSWORD, OPENROUTER_API_KEY, JWT_SECRET_KEY, VAULT_KEY
make -f deploy/sandbox/Makefile up     # ~10-15 min first build
make -f deploy/sandbox/Makefile seed   # DB schema + 9 agents
```

Cloudflare: `sandbox.<domain>` A-record → VM IP, **orange-cloud (proxied)**,
SSL mode Full (strict). Landing page: Cloudflare Pages direct-upload of
`deploy/landing/`.

## 3. Smoke gates (after every deploy)

1. `curl -s https://sandbox.<domain>/api/auth/guest` → JWT (200)
2. Create session → POST the portfolio goal → SSE contains
   `canvas_manifest` (~30–60s)
3. `GET /api/v1/sessions/{id}/audit` with the JWT → evidence package JSON
4. Browser: dashboard renders (MetricCard, LineChart, DataTable, AlertFeed)

## 4. Telemetry & operations

| Signal | Where | Alert threshold |
|---|---|---|
| LLM spend | OpenRouter dashboard / `GET /api/v1/auth/key` (usage field) | > 70% of monthly budget |
| Daily message cap | Gateway logs — `SANDBOX_MAX_DAILY_MESSAGES` rejections | any sustained 429s |
| Guest sessions | Postgres `sessions` table (`user_id` like guest pattern) | review daily at launch |
| Service health | `make -f deploy/sandbox/Makefile status` (all `healthy`) + Cloudflare health check on `/` | any container down > 2 min |
| Rate limiting | nginx `access.log` 429s | single-IP floods |
| Disk | `df -h` (docker images + volumes grow) | > 80% |

- Logs: `make -f deploy/sandbox/Makefile logs` or
  `docker logs sandbox-<service> --tail 200`.
- The one-page daily launch check: status → OpenRouter spend → 429 scan →
  guest session count. ~2 minutes.

## 5. Update & rollback

```bash
cd metaorcha && git pull
make -f deploy/sandbox/Makefile up      # rebuilds changed images
```

Rollback: `git checkout <previous-sha>` and re-run `up`. Postgres/Redis
volumes persist across rebuilds; `make down` does not delete data
(`docker compose down -v` does — never run it in prod).

## 6. Known environment gotchas (hit and fixed locally)

- Debian's npm 9 times out fetching the prisma CLI on some bridge networks —
  all backend Dockerfiles install official Node 20 (npm 10) instead. If a
  build still fails at `prisma generate`, it's network, not code.
- nginx must **preserve** the `/api` prefix (`/api/v1/*` routes); only
  `/api/auth/*` rewrites to `/auth/*`.
- VM must have a **valid OpenRouter key** before seeding — verify with
  `curl -H "Authorization: Bearer $KEY" https://openrouter.ai/api/v1/auth/key`
  (expect 200, not 401).

## 7. Attested profile (local bed) — record a run, refuse a failed one

The example env ships with the attested profile **on**. What it needs and what it
proves, in order:

1. **Key.** `openssl rand -base64 32` → `ATTESTATION_PRIVATE_KEY_B64` in
   `.env.sandbox`. `make -f deploy/sandbox/Makefile check-env` refuses a blank key
   while `RUN_ATTESTATION_ENABLED=true`; an ephemeral key
   (`ATTESTATION_ALLOW_EPHEMERAL_KEY=1`) is for throwaway boots only, because its
   receipts cannot be re-verified after a restart.
2. **Schema.** `make -f deploy/sandbox/Makefile up` now runs `migrate` after the
   containers start (the `attestations` and `attested_settlements` tables come
   from Prisma migrations, not from `init-db`). Re-run `migrate` alone after
   pulling new migrations.
3. **Proof of wiring.** `make -f deploy/sandbox/Makefile attest-check` — both lines
   must read `OK`.
4. **Record.** Send one multi-tool turn through the chat. When the run completes,
   the session's audit download carries the sealed envelope. Verify it without the
   stack: `uvx --from orcha-sdk orcha verify <envelope.json>` → `valid=True`. Flip
   one byte and verify again — the check that breaks is named.
5. **Refuse.** With `SETTLEMENT_REQUIRE_ATTESTATION=true`, a charged call settles
   only after the seal and only if the signed `verdicts[]` carry no `fail`. Run a
   turn whose declared criterion fails: the settle log names
   `CHECK_VERDICT_FAIL`, no credit is written, and the envelope still verifies —
   the refusal is in the signed bytes.

Honesty: the envelope proves what the runtime recorded and that it was not
altered afterwards. It does not prove the work was correct.

Host-run tools: containers resolve `host.docker.internal` to your machine, so an
MCP or A2A server you run locally registers as
`http://host.docker.internal:<port>`.
