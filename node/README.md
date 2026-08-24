# emerge-node (experimental)

> **Status: experimental spike.** Not part of the supported runtime. See
> [ROADMAP.md](../docs/ROADMAP.md) — network-layer capabilities graduate only
> after real adoption of the core runtime.

`emerge-node` is an early prototype of the signing layer for a gossip
sidecar: Ed25519 signed manifest envelopes (canonical JSON, sign/verify) that
two agents could use to exchange capability manifests without a central
registry. The envelope code is what exists today; a gossip transport does
not — it is a gated roadmap phase, not a shipped feature.

It exists to explore what peer discovery *could* look like on top of the
Orcha runtime. It is not a live network, has no transport, no persistence,
no reputation, and no settlement.

```bash
cd node && uv sync && uv run pytest
```
