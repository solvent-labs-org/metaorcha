# Changelog

All notable changes to Metaorcha are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/). `orcha-sdk` is released on its own,
with its own changelog: [sdk/CHANGELOG.md](sdk/CHANGELOG.md).

## [Unreleased]

`orcha-sdk` 0.1.2, 0.1.3, 0.2.0 and 0.2.1 were released from this range; their notes
are in [sdk/CHANGELOG.md](sdk/CHANGELOG.md). #59, #103, #104 and #105 add
database migrations.

### Added

- RFC 0003, the signed run attestation envelope, is accepted, with golden
  vectors and the generator that reproduces them (#57).
- Signed run receipts on the platform, with `RUN_ATTESTATION_ENABLED` on, as it
  is off by default: every completed run is sealed into an RFC 0003 envelope
  (#59).
  - The stored envelope is served over HTTP, unverified, and the finished turn
    offers it as a download (#68, #71).
  - A turn can declare acceptance criteria. Their digest travels in
    `policy_version`, the result is signed as a verdict, and an unknown
    criterion is refused (#73). `exit_zero` judges each step's exit code (#111).
  - Each turn records the model that ran it and where the request went: the
    receipt signs it, and the stream, the transcript, the export and the chat
    show it (#113).
  - Every export states what its signed receipt covers, and what it does not
    (#112).
- Settlement gated on the receipt, with `SETTLEMENT_REQUIRE_ATTESTATION` on: a
  run settles against a valid attestation that matches it, and a refusal is
  audited with the check that failed (#59).
  - A fail verdict refuses settlement (#70).
  - The gate judges every sealed run once, also a run that charged nothing
    (#106).
  - The run tab shows whether the gate settled or refused the run, and names
    the check (#89).
- A local run-attestation playground, `apps/attestation-playground`: it
  produces, tampers with and verifies envelopes in the browser, and CI tests it
  (#60).
- A three-pane chat shell: bots, the transcript and a workspace (#80).
- Connections:
  - anyone signed in can register their own MCP server, with no dev mode (#81);
  - with `CONNECTIONS_ENABLED` on, a tool connects with the person's own token
    (#91), and a write with no declared allow, and every destructive call, waits
    for a person's approval; the receipt says who approved (#93);
  - every capability is classed read, write or destructive, and an unknown one
    counts as destructive (#90);
  - a connection, or its token, can be revoked, and old receipts are left as
    they were (#93);
  - every connector call is a step in the receipt, which commits to the raw
    result and names the connection (#92).
- Offices: each user has a personal office, and routines, connections and
  sessions belong to an office (#104).
- Routines:
  - a turn can be saved as a routine, with its goal, connections, allowed
    writes, model, criteria and schedule (#103);
  - a routine fires on its schedule, through the same runner as a chat turn
    (#105);
  - a firing ends as the gate judged its run (#107);
  - `counts_match`: two connections each report a count, and the run passes
    when both are readable and equal (#108);
  - the routines pane shows the state the server computed, and a refused firing
    names its check (#109);
  - the export says "attested but unsettled" when no gate decided (#110).
- CI: a `ci-complete` job that fails when any job it waits on fails, a frontend
  build, frontend lint as a report, and a dependency vulnerability scan.
- The SDK's wheel and sdist are attested, and attached to their release.

### Changed

- `make check` also covers `sdk/`, as CI does.
- `SECURITY.md` names the CLI `orcha-sdk` (community #47).
- Public copy names the project Metaorcha; `orcha` stays inside component
  identifiers.
- The roadmap is rewritten around a signed run record anyone can check offline
  (#67).
- A deployment that issues receipts refuses to start without a lasting signing
  key: set `ATTESTATION_PRIVATE_KEY_B64`, or `ATTESTATION_ALLOW_EPHEMERAL_KEY=1`
  for development (#72).
- A stdio connection starts a process on the server, so it is for operators,
  with an allow-listed environment (#81).
- The platform builds, signs and judges receipts with the SDK's code, and keeps
  no copy of its own (#115).
- With `RUN_ATTESTATION_ENABLED`, `SETTLEMENT_REQUIRE_ATTESTATION` and
  `CONNECTIONS_ENABLED` off, the stack runs as the stock service, and a test
  holds it (#114).
- CI and code scanning run on pull requests into `community`, the branch for
  outside contributions (#55).
- The dependency update config is restored, with version updates paused and
  advisory alerts on.

### Removed

- Eight legacy demo agents. The four that docs, tests and scripts use stay.

### Fixed

- The UI no longer says "verified" for a check that gates nothing; the tool-run
  badge says "structurally checked" (#75, #89).
- A registered agent's bearer or API-key auth was skipped, because the strategy
  type's case did not match (#91).
- SDK release artifacts attach to the SDK's own tag, never to a platform release
  (#51).
- The `ci-complete` job reports the name branch protection requires.

### Security

- The Gateway signs and verifies tokens with PyJWT in place of python-jose,
  which has an advisory with no fix. A token whose `iat` is in the future is now
  refused (#100).
- The critical and high code scanning alerts on `main` are fixed (#101):
  - a test manifest server read and wrote paths outside its fixtures;
  - a request could choose where an example agent's own calls went;
  - token-store keys and part of a token reached the logs;
  - the native model client was picked by a substring of the URL, and now by
    its host;
  - the Gateway's stream relay sent an exception's text to the browser.
- A test plants a connection token, a stdio variable and a person's own model
  key, and shows that none leaves the transport. It forced four fixes (#93).
- `auth_var` must be an environment variable name, so its value cannot rewrite
  the generated manifest (#81).
- Dependencies with advisories moved to their fixed releases. Where none exists,
  a dated exception records why this code is not exposed (#50, #61, #98).
- The uv image is pinned by digest, agents install from their lockfiles, and the
  gitleaks download is checked against its checksum.

## [0.1.4] — 2026-08-25

Written after the release, from the `v0.1.3..v0.1.4` range: the tag was cut
without a changelog entry. This release ships community contributions.
`orcha-sdk` 0.1.1 was published from the same commit; see
[sdk/CHANGELOG.md](sdk/CHANGELOG.md).

### Added

- CanvasKit renders `markdown_card` content as markdown (community #38).
- Optional stable `manifest.id` on the canvas envelope (community #37).
- Platform tool seeder hardening, with tests.

### Changed

- Roadmap published, and stale docs corrected.
- Dependabot version updates paused pending manual reconciliation (#26).
- Project name corrected in the README (#35).

### Fixed

- `orcha-sdk`: the documented install path now works. The console script is
  named `orcha-sdk`, and CLI help and `--version` follow it (community #33, #34).
- `agents/notion-mcp` pointed at one developer's absolute home path, so the
  agent could not start for anyone else. Non-absolute args now resolve against
  `EMERGE_TOOLS_DIR`, which is the supported form its README documents.
- `poc-e2e.sh` stage 2 had rotted against the live Registry API (data wrapper,
  lowercase `health_status`).
- Gate 6 `__canvas__` allowlist refreshed (community #36).
- Three CI gates that could never pass.
- Frontend, gateway auth, sandbox deploy and request header fixes.

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
