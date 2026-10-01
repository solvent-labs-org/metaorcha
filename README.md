<div align="center">

# Metaorcha

**The open harness for agent work someone else has to trust.**

Give it a goal. Metaorcha plans it, runs it across the agents you already have (MCP, A2A, computer-use), signs every step into a receipt, and lets anyone check the run from the receipt alone.

[![Build](https://github.com/solvent-labs-org/metaorcha/actions/workflows/ci.yml/badge.svg)](https://github.com/solvent-labs-org/metaorcha/actions)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/solvent-labs-org/metaorcha/badge)](https://securityscorecards.dev/viewer/?uri=github.com/solvent-labs-org/metaorcha)
[![Version](https://img.shields.io/badge/version-0.1.3-blue)](CHANGELOG.md)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)

</div>

---

## What a run leaves behind

A run can leave a **receipt**: one JSON envelope ([RFC 0003](docs/spec/rfcs/0003-run-attestation-envelope.md)) that commits to each tool call's arguments and output as SHA-256 hashes, chains the steps together, and is signed with Ed25519. It carries the run's verdicts and timing, never the raw arguments or output.

Anyone can check it, offline, with the published SDK:

```bash
uvx orcha-sdk verify receipt.json     # exit 0 = valid, 1 = invalid
```

The verifier checks the schema, recomputes the step chain and its roots, and verifies the signature against the key in the envelope. No network, no account, no call back to the service that ran it. Change one byte of one step and the roots and signature fail together.

Settlement can be gated on it. With `SETTLEMENT_REQUIRE_ATTESTATION=true`, a charged run settles only when its receipt verifies; a receipt that fails a check, or carries a `fail` verdict, is refused, and the refusal names what failed.

Receipts are off by default. Turn them on with `RUN_ATTESTATION_ENABLED=true` and a signing key (`services/superagent/.env.example` shows how to mint one), then use **Download receipt** in the chat or `GET /api/v1/runs/{run_id}/attestation`. To try it with no setup, the [attestation playground](apps/attestation-playground/) runs an agent, builds the chain live, and lets you tamper with the envelope to watch verification fail.

Binding the signing key to a published identity (`--resolve-did`) is not supported yet: today a receipt proves the run is intact and signed by the key it names.

## See it work

Type a goal. Metaorcha discovers agents, composes MCP, A2A, and computer-use in one run, and renders a CanvasKit dashboard instead of a chat reply.

Every call passes a 7-step execution pipeline: input validation, payment guard, preflight, protocol dispatch, output normalization, checklist update, settlement. Each step gets a verdict, and the run downloads as a JSON evidence package (per-step agent, protocol, verdict, cost, timing) alongside its receipt.

Output is not a chat bubble. Agents return a declarative [CanvasKit](docs/spec/canvaskit.md) manifest and the runtime renders metric cards, charts, tables, and alert feeds as a live dashboard. Structured output persists, and structured output can be checked.

**Hero goal (2 protocols, no keys):** *"Show me my portfolio performance, and screenshot the Alpaca dashboard"* → [`finance-dashboard-agent`](agents/finance-dashboard-agent/) over MCP, which `./scripts/run-all.sh` starts for you, plus [`computer-use-agent`](agents/computer-use-agent/) over COMPUTER_USE. The second needs no process of its own: its manifest is a placeholder and the SuperAgent's built-in mock backend answers it, so the run needs no credentials and reaches nothing external. Set `COMPUTER_USE_BACKEND` to swap in a real provider without touching the manifest.

**A third protocol:** `./scripts/poc-e2e.sh` publishes [`poc-probe-agent`](agents/poc-probe-agent/), a paid A2A agent built entirely on the `emerge` SDK, and drives it through registration, discovery, execution, verification, and settlement.

**Try it:** clone and run `./scripts/run-all.sh`, or bring up the [sandbox stack](deploy/sandbox/README.md) locally with `make -f deploy/sandbox/Makefile up`. Demo portfolio data is illustrative, no brokerage connection required. (A hosted public sandbox is not currently up — see the [roadmap](docs/ROADMAP.md).)

**Prove it yourself:** `./scripts/poc-e2e.sh` asserts verification, retry, and settlement end to end.

## Register an agent in 4 lines

> Metaorcha ships the **`orcha-sdk`** package (import name `emerge`). `orcha-sdk init` scaffolds an agent, `orcha-sdk run` serves it and registers it with the runtime. No clone required: `uvx orcha-sdk init my-agent`.

```python
import emerge

@emerge.agent(name="My Agent", description="What I do")
def handle(task: str) -> str:
    return f"handled: {task}"
```

```bash
orcha-sdk run     # serve locally and register with the runtime
```

## Quickstart

Just building an agent? Zero clone, no infrastructure:

```bash
uvx orcha-sdk init my-agent && cd my-agent && uvx orcha-sdk run
```

That serves a live A2A agent on `:8900` — `/health`, `/.well-known/agent.json`,
and JSON-RPC `message/send` all answer immediately. Registration needs a
registry; without one running, `run` says so and keeps serving. Start the
runtime below to register, or use `orcha-sdk run --no-register`.

Running the full runtime (registry, planner, orchestrator, dashboard):

```bash
git clone https://github.com/solvent-labs-org/metaorcha && cd metaorcha
./scripts/run-all.sh        # infra + all services + seed agents
```

Per-service details live in the [docs](https://metaorcha.ai/docs).

Bring any OpenAI-compatible LLM key (Gemini and Groq free tiers work) or run models locally through Ollama. Payments run in mock mode by default: no wallet, no closed-service dependency.

## What we didn't build

- **An agent framework.** Bring the agent you have, over MCP, A2A, or computer-use. The harness only cares about the run.
- **A chat UI.** Agents return a [CanvasKit](docs/spec/canvaskit.md) manifest, and the runtime renders it as a dashboard.
- **A model.** Any OpenAI-compatible key works, or run models locally through Ollama.
- **A control plane you rent.** Agents stay services you own and run, and the whole stack runs on your machine.

## Terms

| Term | Meaning |
|------|---------|
| **Harness** | Everything around the agents: planning, routing, identity, verification, rendering |
| **Handler** | A protocol bridge. MCP, A2A, and computer-use ship today |
| **Verdict** | The `pass` / `fail` / `warn` record a check leaves on a step or a run |
| **Receipt** | The signed, offline-verifiable record of a run ([RFC 0003](docs/spec/rfcs/0003-run-attestation-envelope.md)) |
| **Evidence package** | The unsigned JSON export of a run: per-step agent, protocol, verdict, cost, timing |
| **CanvasKit** | The declarative manifest agents return, rendered as live UI |

## Architecture

Protocols are plumbing: the harness speaks MCP, A2A, and computer-use so your agents do not have to share one. Goal in, receipt out:

```
Goal
 └─► Registry ──► Planning & Discovery ──► SuperAgent
                                               │
                         ┌─────────────────────┼─────────────────────┐
                         ▼                     ▼                     ▼
                   MCP handler           A2A handler          COMPUTER_USE handler
```

(`protocol.type: "acp"` is still accepted in `emerge.yaml` and routes through
the A2A handler, a compatibility alias rather than a fourth independently-bridged
protocol. The `emerge.yaml` schema and its governance rules live in
[docs/spec/](docs/spec/).)

<details>
<summary>Service map</summary>

| Service | Port | Role |
|---------|------|------|
| Registry | 8000 | Agent registration + gRPC |
| Planning & Discovery | 8001 | Vector search + LLM planner |
| SuperAgent | 8002 | LangGraph orchestration engine, protocol dispatch |
| Gateway | 8080 | Auth + BFF + mock payments |
| Frontend | 3000 | React chat + CanvasKit renderer |

</details>

## The harness layer

MCP and A2A standardized how agents talk. Models are converging. Neither solves the harder problem: discovering agents on any protocol, composing them into one run, and checking what each step actually did.

Today that layer is hand-built glue code inside most serious stacks. The identity half is being answered: signed credentials that say who an agent is and what it may spend, checked before it acts. Evidence of the work is the newer half. Several designs for signed, hash-chained run records now exist, they do not agree on a wire format, and on the shipping payment paths none of them gates the money: the seller asserts delivery and the charge goes through.

Metaorcha is that layer, open and inspectable. Neutral ground: agents stay external services that you own and run. The harness plans, routes, verifies, and renders. It does not embody any single agent.

<img src="https://metaorcha.ai/diagrams/missing-layer.svg" alt="MCP, A2A, and computer-use stacks, today connected by hand-written glue code" width="100%" />

## Contribute

| What | Where | Why it matters |
|------|-------|----------------|
| **New bridge** | `templates/your-first-bridge/` | Adds a protocol, highest leverage contribution |
| **New agent** | `agents/` | Grows the fleet, stress-tests the runtime |
| **CanvasKit component** | `frontend/src/components/canvas/` | New dashboard primitives for agent output |

→ [CONTRIBUTING.md](CONTRIBUTING.md) · [Write a bridge](templates/your-first-bridge/) · [Open a RFC](https://github.com/solvent-labs-org/metaorcha/issues/new?labels=rfc)

## What's next

**Sandbox hardening + UIUX (v0.2.0)**, full trajectory in the [roadmap](docs/ROADMAP.md).

---

<div align="center">Apache 2.0 · <a href="https://metaorcha.ai/roadmap">Roadmap</a></div>
