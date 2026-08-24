<div align="center">

# Metaorcha

**The open harness for multi-protocol agent systems.**

Give it a goal. Metaorcha plans, routes, verifies, and renders across agents speaking different protocols in a single run.

[![Build](https://github.com/solvent-labs-org/metaorcha/actions/workflows/ci.yml/badge.svg)](https://github.com/solvent-labs-org/metaorcha/actions)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/solvent-labs-org/metaorcha/badge)](https://securityscorecards.dev/viewer/?uri=github.com/solvent-labs-org/metaorcha)
[![Version](https://img.shields.io/badge/version-0.1.3-blue)](CHANGELOG.md)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)

</div>

---

## The missing layer

MCP and A2A standardized how agents talk. Models are converging. Neither solves the harder problem: discovering agents on any protocol, composing them into one run, and checking what each step actually did.

Today that layer is hand-built glue code inside every serious stack. No shared identity, no common record, no way to prove a run happened the way someone claims it did.

Metaorcha is that layer, open and inspectable. Neutral ground: agents stay external services that you own and run. The harness plans, routes, verifies, and renders. It does not embody any single agent.

<img src="https://metaorcha.ai/diagrams/missing-layer.svg" alt="MCP, A2A, and computer-use stacks, today connected by hand-written glue code" width="100%" />

## See it work

Type a goal. Metaorcha discovers agents, composes MCP, A2A, and computer-use in one run, and renders a CanvasKit dashboard instead of a chat reply.

Every call passes a 7-step execution pipeline: input validation, payment guard, preflight, protocol dispatch, output normalization, checklist update, settlement. Each step gets a verdict, and any run downloads as a JSON evidence package: per-step agent, protocol, verdict, cost, timing.

Output is not a chat bubble. Agents return a declarative [CanvasKit](docs/spec/canvaskit.md) manifest and the runtime renders metric cards, charts, tables, and alert feeds as a live dashboard. Structured output persists, and structured output can be checked.

**Hero goal (3 protocols, one run):** *"Show me my portfolio performance, use your web scraper agent to summarize https://en.wikipedia.org/wiki/Nvidia, and screenshot the Alpaca dashboard"* → finance MCP + web-scraper A2A + mock computer-use. Verified live, 5/5 runs, best wall clock 13s.

**Try it:** clone and run `./scripts/run-all.sh`, or bring up the [sandbox stack](deploy/sandbox/README.md) locally with `make -f deploy/sandbox/Makefile up`. Demo portfolio data is illustrative, no brokerage connection required. (A hosted public sandbox is not currently up — see the [roadmap](docs/ROADMAP.md).)

**Prove it yourself:** `./scripts/poc-e2e.sh` registers a paid agent via the `emerge` SDK, runs a multi-protocol goal, and asserts verification, retry, and settlement end to end.

## Terms

| Term | Meaning |
|------|---------|
| **Harness** | Everything around the agents: planning, routing, identity, verification, rendering |
| **Handler** | A protocol bridge. MCP, A2A, and computer-use ship today |
| **Verdict** | The pass/fail record every pipeline step carries |
| **Verified run** | A downloadable JSON evidence package for a run |
| **CanvasKit** | The declarative manifest agents return, rendered as live UI |

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

## Architecture

Goal in, verified run out:

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
