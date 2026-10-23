# Changelog: MetaBot

All notable changes to MetaBot (`apps/metabot`) are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); MetaBot uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Every run records what it spent: its tokens of each kind, what they cost,
  and where the figure comes from. That is the engine's own report, the
  listed rates, your rates, a local model at $0.00, or your plan, which is
  not priced. The receipt signs it as a `usage` verdict, with a digest of the
  rates, and `metabot ask` prints it beside the receipt.
- `metabot ask --max-cost DOLLARS` ends the run once it has cost that much.
  The receipt signs the cap as a `budget` verdict. A run MetaBot cannot price
  does not start under a cap.
- `metabot cost` totals past runs by engine and model.
- `prices` in `config.json` gives a model its rates, in dollars per million
  tokens.
- `metabot doctor --models` checks whether each local model can call
  MetaBot's tools: by itself, by text that MetaBot reads, or held to a schema
  one step at a time. It keeps what it finds, and the engine asks each model
  the way that works.
- A local model that writes Python's triple quotes unescaped inside a JSON
  tool call has the call read, with the quotes as text.
- When a local model writes calls one after another and the last one breaks
  off before its JSON ends, the complete ones are made and the broken one is
  not.
- A tool call a local model writes in a code fence inside a longer answer is
  never made. MetaBot asks the model for the call alone, twice at most, and
  then fails the run, so an answer that shows a call and makes none does not
  end "ok".
- `metabot ask --ladder` starts on the cheapest rung, and climbs only when
  the record shows the rung failed. It stops instead after a declined call or
  a write that went through. `metabot ladder` shows the rungs: by default the
  local model, once probed, then the `build` role. `--max-cost` covers the
  whole ladder, and each rung is a run with its own receipt.

### Changed

- A `claude` run counts its tokens message by message as it goes, so a token
  budget can stop it before it ends. The result's count replaces the running
  one.
- When a local model writes its tool call as text, in a code fence or in
  tags, MetaBot makes the call, through the gate like any other. A tool name
  that is nearly right runs the one tool it can mean. The journal notes each
  of these. An answer that is a call MetaBot cannot read fails the run, where
  it was shown as the answer.

## [0.1.2] — 2026-10-21

### Security

- A `claude` run no longer loads the engine's settings files. In 0.1.0 and
  0.1.1, a repository's committed `.claude/settings.json` or
  `.claude/settings.local.json` took effect inside a run: its hooks and key
  helper ran without a gate decision, and its `env` could send the run's
  model calls, with your login, to another address. MetaBot now passes its
  gate hook and plugins itself. From your own settings file it keeps your
  model, your effort for each model, your telemetry and error-reporting
  opt-outs, and your deny rules. Nothing else in that file is passed on.
- The `openai` engine's key no longer reaches its shell or the run's record.
  In 0.1.0 and 0.1.1, its shell tool ran with your whole environment, so a
  model that ran `env` was shown the key that `openai_key_env` names, and the
  record could keep it. The shell now runs without that variable, and the
  key is replaced in the steps, the journal, the receipt and the run index,
  also when the model reads it from a file or repeats it in its answer.

### Changed

- A `claude` run no longer loads `CLAUDE.md` files, or skills, agents,
  commands, hooks and plugins from the project or your engine settings. A
  login set in the settings file is not read: sign in with `/login` or a key
  in the environment. Your `sandbox` settings do not apply in a `claude` run.

### Fixed

- A run whose folder path is too long for a Unix socket puts the socket in a
  temp folder. Three paths left that `mb-*` folder behind; every run now
  gives it back.
- `metabot receipts verify` no longer creates `~/.metabot` and its receipts
  folder on a machine that has none.

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
