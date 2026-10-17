# Changelog: MetaBot

All notable changes to MetaBot (`apps/metabot`) are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); MetaBot uses
[Semantic Versioning](https://semver.org/).

## [0.1.1] — 2026-10-18

### Changed

- A plain `metabot ask`, with no `-e`, `-m`, `--role` or `--suggest`, takes
  the `fast` role's model when that role runs on the default engine and no
  model is set for that engine. The engine stays the default one, so where
  the work goes does not change.

### Added

- When the prompt names files in the project and the run made no tool calls,
  MetaBot says so beside the answer: the receipt shows no read of them. A
  resumed run says nothing, since an earlier turn may have read them.

### Fixed

- A call that came back with an error, such as a command that exits
  non-zero, was reported as "failed". The line beside the answer now says how
  many calls did not succeed, and the trace shows a multi-line error on one
  line, where it printed `\n` between the lines.

## [0.1.0] — 2026-10-17

Initial release, published as `orcha-metabot`; it installs the `metabot` command.
The [README](README.md) says what MetaBot does, and the limits
it states.
