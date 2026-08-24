# Contributing to Orcha

Thanks for your interest in Orcha — open **agent orchestration and observability**
infrastructure.

This is not a model benchmark
repo — it's about **routing, executing, observing, and distributing** agents
across MCP, A2A, and COMPUTER_USE (ACP manifests are accepted as a
compatibility alias and routed through the A2A handler).

Contributions we **eagerly welcome** without prior discussion:

- **Bridges** — a new protocol handler so the runtime can orchestrate agents
  that speak something we don't support yet (n8n, LangGraph, OpenAPI, …). This
  is the contribution we most want. Start from
  [`templates/your-first-bridge/`](templates/your-first-bridge/) and read
  the [bridges guide](https://metaorcha.ai/docs).
- **Agents** — a new example agent that registers against the local registry.
  Scaffold one with `uvx orcha-sdk init my-agent` (the template ships inside the
  package, at
  [`sdk/src/emerge/templates/your-first-agent/`](sdk/src/emerge/templates/your-first-agent/)),
  then read the [quickstart](https://metaorcha.ai/docs).
- **Observer / attestation experiments** — the `ExecutionObserver` seam and the
  validator reference code (`services/validator/`). See the [roadmap](https://metaorcha.ai/roadmap).

> **Core engine changes** (the SuperAgent execution pipeline, the registry
> contract, the planner) need an issue **first**. Open one describing the
> problem before sending a PR so we can agree on the approach — the
> `emerge.yaml` spec in particular is treated as frozen-by-default (see below).

## Ground rules

- **The spec is the ERC-20.** `emerge.yaml` is versioned (`schema_version`) and
  validated against [`docs/spec/emerge-yaml.schema.json`](docs/spec/emerge-yaml.schema.json).
  Once external agents exist, breaking the spec breaks everyone. Spec changes
  go through the RFC process in [`docs/spec/governance.md`](docs/spec/governance.md).
- **Run in mock mode.** The public runtime runs fully with `PAYMENT_MODE=mock`
  and no private dependency. PRs must not introduce a hard dependency on any
  closed/hosted service.
- **DID namespace is `did:orcha:agent:*`** (user agents) / `did:orcha:system:*`
  (platform tools). Don't reintroduce other namespaces.

## Development setup

```bash
git clone git@github.com:solvent-labs-org/metaorcha.git
cd metaorcha
make install            # install Python + JS deps via uv / npm
make prisma-generate
make grpc-generate
./scripts/run-all.sh    # bring the local stack up
```

See the [quickstart](https://metaorcha.ai/docs) for the 5-minute path, or
the [setup guide](https://metaorcha.ai/docs) for manual service-by-service setup.

## Commit messages

Keep them **short and scannable** — one logical change per commit.

```
<type>(<scope>): <what changed>
```

| Type | Use for |
|---|---|
| `feat` | New behavior users or operators touch |
| `fix` | Bug fix |
| `docs` | README, ROADMAP, vision, devdocs |
| `refactor` | Code move/rename, no behavior change |
| `test` | Tests only |
| `chore` | CI, deps, Makefile |

**Scopes (examples):** `sdk`, `superagent`, `registry`, `gateway`, `node`, `validator`

**Examples:**

```
feat(node): signed manifest envelope helpers
docs: simplify readme quickstart
fix(settlement): three-way fee split when validator bps set
test(validator): FulfillmentRecorder attestation
```

No `Co-authored-by` trailers unless you actually pair-programmed.

## Before you open a PR

- `make check` passes (lint + format + tests).
- New code has tests. Bridges and agents include a minimal example + manifest.
- No secrets, credentials, or client references in the diff.
- Public-facing text says **Orcha**, not any internal brand.

## Writing a bridge

A bridge is a subclass of `AgentHandler` that adds one protocol to the runtime. No prior discussion needed — just open a `feat/bridge-<protocol-slug>` branch and submit.

```
feat/bridge-openapi       feat/bridge-n8n
feat/bridge-langchain     feat/bridge-grpc
```

Steps:

1. Copy `templates/your-first-bridge/` → `services/superagent/src/superagent/handlers/<protocol>_handler.py`
2. Implement `send_task()` — receive a dict, return a string; prefix hard errors with `Error:`
3. Add one `if protocol == "YOUR_PROTOCOL":` block in `_dispatch()` (`middleware/pipeline.py`)
4. Add a registry adapter so `protocol.type: YOUR_PROTOCOL` is valid in `emerge.yaml`
5. Ship an example agent and a smoke test in `tests/integration/`

See the [bridges guide](https://metaorcha.ai/docs) for the full contract, the "Wanted Bridges" wishlist, and the PR checklist.

## Reporting bugs / requesting bridges

Use the issue templates: **Bug report**, **Bridge request**, or
**Agent submission**. For security issues, do **not** open a public issue —
see [`SECURITY.md`](SECURITY.md).

## License

By contributing you agree your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
