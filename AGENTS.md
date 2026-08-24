# AGENTS.md

Instructions for AI coding agents (Cursor, Claude Code, Codex, aider, etc.)
working in this repository. Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md).
Project trajectory: [metaorcha.ai/roadmap](https://metaorcha.ai/roadmap).

- The default branch is `main`. Branch naming: `<type>/<description>`
  (e.g. `feat/sandbox-deploy`, `fix/registry-health`). Never push to
  `main` directly without review.

## Two-repo topology

- **`solvent-labs-org/metaorcha-internal`** (private, this repo) — the working
  repo. All branches, full history, `docs-local/`, strategy docs.
- **`solvent-labs-org/metaorcha`** (public) — the OSS face. Single orphan
  `main` only: production code, sanitized downstream export of this
  repo's `main`. Design/planning docs stay private.
- Export flow: internal `main` → sanitize (brand sweep, exclude internal
  paths, gitleaks) → force-update public orphan `main` per release. The
  export remote in the export worktree is named `public`; **never push
  any ref other than the sanitized orphan `main` to it.** KY-A content
  must never cross into the public repo — it is hackathon/product work,
  private by design.

## Commands

```bash
make install                # install Python + JS deps (uv / npm)
./scripts/run-all.sh        # infra + all services + seed agents
make check                  # lint + format + tests, run before every PR
./scripts/poc-e2e.sh        # end-to-end proof: register → run → verify → settle
```

> Port note: the `deploy/sandbox/` Docker stack binds host 8000
> (`sandbox-registry`) and will silently shadow a local Registry — stop it
> before running the stack locally.

## Contracts that MUST hold

- **Canvas envelope** — agents returning rich output emit the exact
  `__canvas__` envelope (`manifest.version: "1.0"`, snake_case component
  types, flat fields). Spec: `docs/spec/canvaskit.md`.
- **Protocol dispatch** — never hard-code a protocol backend; use the
  env-var swap pattern in `services/superagent/src/superagent/handlers/`.
  Post-execution hooks go through the `ExecutionObserver` seam
  (`middleware/pipeline.py`), never inline in `execute_agent_calls.py`
  or `runner.py`.
- **DID namespace** — `did:orcha:agent:*` for user agents,
  `did:orcha:system:*` for platform tools. Never `did:emerge:`,
  `did:metaorcha:`, or a bare name.
- **emerge.yaml schema** (`docs/spec/emerge-yaml.schema.json`) is versioned;
  breaking changes need an RFC in `docs/spec/governance.md`.

## OSS Hard Rules (non-negotiable)

1. **Mock-first** — full stack runs with `PAYMENT_MODE=mock`; mock fallback
   for anything external.
2. **The name is Metaorcha** (`docs/brand.md`). Public copy says "Metaorcha".
   "Orcha" is not a standalone name — it appears only inside component
   identifiers (`orcha-sdk`, `orcha-attest`, `did:orcha:*`, `ORCHA_*`), never
   as an H1, a page title, or the subject of a sentence. KYA = category term.
   Never an internal name in any committed file.
3. **No secrets in files** — `.env.*` is gitignored except `.env.example` /
   `deploy/sandbox/.env.sandbox.example`. CI runs gitleaks on every PR.
4. **No token announcement** — no tokenomics or launch timelines anywhere.
5. **Closed-service adapters** are named only in adapter-specific internal
   docs — never in product text, README, public docs, or code comments.

## Do NOT Edit (Generated Files)

- `common/proto/src/*_pb2.py`, `*_pb2_grpc.py`, `*_pb2.pyi` — regenerate via `make grpc-generate`
- `common/database/src/generated_client/` — regenerate via `prisma generate`

## Before Opening a PR

- `make check` passes (lint + format + tests)
- New code has tests; bridges/agents include a minimal example + manifest
- No secrets or client references in the diff

## BMAD-METHOD

Structured planning skills may be installed locally (`_bmad/`, `.agents/skills`,
`.claude/skills`); these are machine-local and not tracked here. When present,
start with `bmad-help`.
