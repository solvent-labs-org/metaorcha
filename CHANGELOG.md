# Changelog

All notable changes to Orcha are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/).

## [0.1.3] — 2026-08-05

Docs-only release. No functional changes.

### Changed

- README reoriented around the missing-layer thesis: new problem framing,
  a terminology table (harness, handler, verdict, verified run, CanvasKit),
  and a site-hosted diagram. Quickstart, SDK, and contribution paths unchanged.

## [0.1.2] — 2026-08-05

Repo hygiene release. No functional changes.

### Changed

- Tidied repo root: `docker-compose.{local,dev}.yml` moved to `deploy/`,
  `PRODUCT.md` and `ROADMAP.md` moved to `docs/`, sandbox env template moved
  to `deploy/sandbox/.env.sandbox.example` (live `.env.sandbox` now lives
  beside it — move yours once: `mv .env.sandbox deploy/sandbox/`).
- Slim public tree: narrative docs (guides, architecture, SRS, roadmap) no
  longer ship in the repo — the guides live at
  [metaorcha.ai/docs](https://metaorcha.ai/docs) and the roadmap at
  [metaorcha.ai/roadmap](https://metaorcha.ai/roadmap). The repo ships the
  engine plus the versioned `docs/spec/` contract.
- `AGENTS.md` slimmed to the operational rules.
- Remaining `azank1/orcha` URLs repointed to `solvent-labs-org/metaorcha`
  (SDK metadata, gateway/mailer strings, docs, issue templates).

### Fixed

- `docker-compose.dev.yml`: quoted a date-like env value YAML mis-parsed as
  a timestamp.

## [0.1.1] — 2026-08-04

Sanitization fix, no functional changes. Supersedes v0.1.0.

## [0.1.0] — 2026-08-03

Initial public release.

### Added

- Multi-protocol agent orchestration runtime: Registry, Planning & Discovery
  (hybrid search + 5-stage DAG planner), SuperAgent (LangGraph ReAct loop),
  Gateway (auth + BFF + mock payments), CanvasKit live-dashboard frontend.
- `emerge` SDK: agent registration decorator, CLI, A2A server helpers.
- Flag-gated DAG execution (`DAG_PLANNER_ENABLED`, default off): complex goals
  route through a hybrid heuristic+semantic gate to the planner and execute as
  a planned sequential workflow through the same middleware pipeline.
- Flag-gated CDV step verification (`CDV_VERIFICATION_ENABLED`, default off):
  per-step deterministic scoring with per-run SQLite store and an adaptive
  stop backstop.
- Hosted sandbox (Beta) — `deploy/sandbox/` Docker stack with spend caps.
- OpenSSF Scorecard workflow + badge, gitleaks secret scanning, launch-gate CI.

### Notes

- Sandbox is Beta: session errors are under active debugging (fixed in 0.2.0).
- `PAYMENT_MODE=mock` runs the full stack with no external paid dependency.
