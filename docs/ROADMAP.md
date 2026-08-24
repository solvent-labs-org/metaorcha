# Orcha Roadmap

This is the public trajectory for Orcha. It describes direction, not dated commitments. Each phase is gated on the phase before it being validated by real adoption — we build the next layer only when the current one is earned.

---

## ✅ v0.1.x — OSS runtime (current)

The multi-protocol orchestration runtime, fully usable locally in mock payment mode.

- Core services: Registry, Planning & Discovery, SuperAgent, Gateway
- Protocols orchestrated in one run: **MCP**, **A2A**, **COMPUTER_USE** (ACP as an A2A-routed compatibility alias)
- Execution pipeline: input validation, credential vault + auth cascade (OAuth), output normalization, human-in-the-loop interrupts, per-call payments (mock mode by default)
- 7 example agents + agent/bridge templates
- `orcha-sdk` CLI (`init` / `run` / `publish`, with `emerge` kept as an alias) and the `@emerge.agent` SDK decorator
- Versioned `emerge.yaml` spec with JSON Schema + RFC governance
- `ExecutionObserver` seam — a no-op hook today; the extension point for post-execution observers
- Sandbox stack — `deploy/sandbox/` Docker compose with spend caps, self-hostable today. The hosted origin is **currently offline**; bringing it back is v0.2.0 work.

---

## 🔧 v0.2.0 — Sandbox hardening + UIUX (gated on v0.1.x validation)

Make the sandbox boring and the demo honest.

- Bring the hosted sandbox origin back up
- Session-error root-cause fixes in the sandbox
- Beta UX polish; demo re-record against the hardened stack
- DAG executor hardening for parallel and dependent step execution; parallel tool execution decision
- Output verification and semantic judging; retry and fallback policies
- Context manager for long-running multi-step tasks

---

## v0.3.0 — Harness depth

- BYOK / managed keys for model providers
- Coding-agent wrapper (ship-agent) and context-packer bridge
- Signed releases

---

## v1.0.0 — Stability commitment

- API/schema freeze policy and upgrade guide
- Enforced branch protection + SAST
- Real adoption signal before the 1.0 cut

---

## 🌐 Post-beta — Network effects (gated on sandbox beta adoption)

Directional only — no shipping claims. Explored once the dual-mode sandbox
(user runs + developer test bench) has real usage.

- Scheduled runs (cron-style goals)
- Swarm / fan-out execution patterns
- Deep-research vertical and richer CanvasKit component sets
- Composability over the harness: meta-agentic learning across runs, shared
  agent capability graphs, reputation signals through the ExecutionObserver seam

---

Have an opinion on direction? Open a
[discussion](https://github.com/solvent-labs-org/metaorcha/discussions), file an
[issue](https://github.com/solvent-labs-org/metaorcha/issues), or join us on
[Discord](https://discord.gg/orcha). Contributing guide:
[CONTRIBUTING.md](../CONTRIBUTING.md).
