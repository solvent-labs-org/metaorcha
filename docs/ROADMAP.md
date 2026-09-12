# Metaorcha Roadmap

This is the public trajectory for Metaorcha. It describes direction, not dated
commitments. Each phase is gated on the one before it being earned in real use.

The product is an attested bot on the open harness: you talk to it, every
finished turn leaves a signed run record, and anyone can check that record
offline. Use whatever model you like. The Metaorcha bot is the one whose work
you can prove.

A signed record proves **attribution** — who produced this, with which tools,
in what order, unedited since. It does not, by itself, prove **acceptance**
(that the answer was any good). Acceptance needs a declared, machine-checkable
criterion. That is the next layer, not a synonym for a receipt.

Verification never requires a Metaorcha server. The sealed envelope is the
artifact; `orcha-sdk` checks it locally.

---

## v0.1.x — OSS runtime and the signed run record (current)

The multi-protocol orchestration runtime, fully usable locally in mock payment
mode. The Know Your Agent (KYA) record is specified and checkable; we did not
coin the term (Skyfire, 2024).

- Core services: Registry, Planning & Discovery, SuperAgent, Gateway
- Protocols in one run: **MCP**, **A2A**, **COMPUTER_USE** (ACP as an A2A-routed
  compatibility alias)
- Execution pipeline: input validation, credential vault + auth cascade,
  output normalization, human-in-the-loop interrupts, per-call payments
  (mock mode by default)
- Example agents and agent/bridge templates
- `orcha-sdk` CLI — `init`, `run`, `publish`, `validate`, and **`verify`**
  (RFC 0003 run-attestation envelope, offline). The documented invoke is
  `uvx --from orcha-sdk orcha verify envelope.json`. The Python import remains
  `emerge`.
- Versioned `emerge.yaml` spec with JSON Schema and RFC governance
- `ExecutionObserver` seam — post-execution hooks, including the optional
  run-attestation producer (`RUN_ATTESTATION_ENABLED`)
- RFC 0003 envelope, golden vectors, producer, and settle gate — in tree
- Attestation playground under `apps/attestation-playground/` — a **teaching
  artifact**. Clone it and run it locally. It is not a hosted product.
- `deploy/sandbox/` — a self-hostable Docker compose with spend caps. A
  hosted public sandbox is **not** a product line.

---

## Next — attested turns you can hold

Close the gap between "the run was sealed" and "a person holds the bytes."

- HTTP fetch of a sealed envelope by run (the route serves bytes; it does
  not verify)
- A download from the finished chat turn, then
  `uvx --from orcha-sdk orcha verify`
- A first vertical whose success criterion is machine-checkable — recommended:
  developer/repo work (`tests pass`, `build green`, `diff applies`) — so a
  structurally valid output can still be refused, the dependent action stops,
  and the refusal stays on the record
- Model-agnostic routing (bring your own key)

Not on this phase: hosting the playground, reviving a public sandbox origin,
or competing on model quality.

---

## Later — harness depth

- DAG executor hardening for parallel and dependent steps
- Context management for long-running multi-step tasks
- Coding-agent wrappers and a context-packer bridge
- Retry, fallback, and declared-criterion checks beyond the first vertical
- API/schema freeze, upgrade guide, and a real adoption signal before a 1.0 cut

---

## Direction — the same record, more than one host

Directional only — no shipping claims.

The record format, the verifier, the harness, and the bot stay open. Hosted
convenience (a managed station, stored history) is a later product. Membership
is conformance to the record, not to our binary. Settlement between strangers
and a public forge are not promised here.

---

Have an opinion on direction? Open a
[discussion](https://github.com/solvent-labs-org/metaorcha/discussions), file an
[issue](https://github.com/solvent-labs-org/metaorcha/issues), or join us on
[Discord](https://discord.gg/orcha). Contributing guide:
[CONTRIBUTING.md](../CONTRIBUTING.md).
