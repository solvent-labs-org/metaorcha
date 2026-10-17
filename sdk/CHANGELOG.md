# Changelog: orcha-sdk

All notable changes to the `orcha-sdk` package are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the package
uses [Semantic Versioning](https://semver.org/). Releases are tagged
`sdk-vX.Y.Z`. The repo's own releases are in the [root changelog](../CHANGELOG.md).

## [0.2.1] — 2026-10-16

### Changed

- Licensed under Apache-2.0, like the rest of the repo, with the licence file
  in the package. Releases up to 0.2.0 stay under MIT, as they were published.
- Docs and help name the product Metaorcha, and each engine by its command.

### Fixed

- A new folder for the signing key is made with mode 0700.
- Two processes that make the signing key at once end with one key. The one
  that loses the race waits for the whole key, and a key that no writer
  finished is refused as empty or half written.
- The registry client, which `run` and `publish` use, refuses a registry URL
  that is not http or https.

## [0.2.0] — 2026-10-06

### Added

- `orcha record`: signed run receipts, made locally with no service.
  - `orcha record seal` builds and signs an RFC 0003 run attestation envelope
    from a run description.
  - `orcha record keygen` writes an Ed25519 signing key file with mode 0600,
    and never prints the seed.
  - `orcha record hook` is a harness hook: it journals each tool call, and
    seals the receipt when the session stops.
- Declared acceptance criteria, `exit_zero` and `citations_required`: a run's
  receipt carries their digest and the result.
- The `orcha-record` plugin for `claude`, listed in this repo's marketplace,
  with an install path that needs no clone.
- `orcha verify` says what a receipt covers: a Coverage block, or `coverage`
  with `--json`. `emerge.run_attestation.coverage_statement` computes it from
  the envelope, whether or not the envelope verifies.
- Modules for a producer, with no service and no network:
  - `emerge.record` builds and signs envelopes byte for byte as the platform's
    producer does, and both are held to the same golden vector;
  - `emerge.preimage` turns a tool call's raw result into the value its
    `output_hash` commits to, with credentials redacted;
  - `emerge.criteria` holds the criteria digest and the evaluation rules that
    every producer reads;
  - `emerge.journal` keeps a run's raw steps on the machine, so a step can be
    revealed later against its hashes.

### Fixed

- On Python 3.10, every console script failed at import in 0.1.1 to 0.1.3,
  because the CLI used a name new in 3.11. The package runs on the 3.10 it
  declares, and a static test guards the floor.

## [0.1.3] — 2026-09-12

### Added

- `orcha-sdk verify`: an offline verifier for RFC 0003 run attestation
  envelopes, with golden tests.
- `orcha`, a shorter name for the CLI.

## [0.1.2] — 2026-09-01

### Changed

- The PyPI page and the installed package say Metaorcha.

## [0.1.1] — 2026-08-25

### Fixed

- The documented install path works: the console script is named `orcha-sdk`,
  and its help and `--version` follow it (community #33, #34).

## [0.1.0] — 2026-08-04

Initial release.
