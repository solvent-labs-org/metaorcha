<div align="center">

# Metaorcha

A local bot for the coding agents you already use.<br>
Every connection gated, every run signed, every receipt checkable offline.

[![CI](https://github.com/solvent-labs-org/metaorcha/actions/workflows/ci.yml/badge.svg)](https://github.com/solvent-labs-org/metaorcha/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/solvent-labs-org/metaorcha/badge)](https://securityscorecards.dev/viewer/?uri=github.com/solvent-labs-org/metaorcha)

</div>

## MetaBot

MetaBot runs your task on a coding agent you already sign in to, or on a model
server on your machine, and keeps a signed record of what happened.

- **Your agents, your plan.** It drives the command line agents you already
  use, or a local model. It brings no model of its own.
- **A gate on the calls that pass through it.** Reads run, writes ask, and a
  destructive call runs when you type `yes`.
- **A receipt for every run.** Each run ends in a receipt signed with your key.
  Anyone can check it offline, with no server of ours.
- **Offices of agents.** Several agents work one task, each on its own branch
  in its own worktree. MetaBot never pushes, and it never commits to your
  branch.
- **Routines and a page.** Goals run on a schedule, and `metabot ui` shows
  runs, asks, offices and routines on `127.0.0.1`.

## Install

```sh
uv tool install orcha-metabot
```

It needs Python 3.11 or newer and a POSIX system, and it is tested on Linux.
Sign in to each agent you want it to drive, the same way you do without it.

## Start

```sh
metabot init                       # sets up your signing key and default engine
metabot ask "what is in this directory?"
metabot receipts verify <run-id>   # checks the signature and every hash
```

[MetaBot's README](apps/metabot/README.md) covers engines, connections,
offices, routines, the web page, and the limits it states.

## Check a receipt

A receipt is a JSON file. Anyone can check one with the SDK, without MetaBot
and without us:

```sh
metabot ask --project-receipt "what is in this directory?"
uvx --from orcha-sdk orcha verify .orcha/receipts/<run-id>.json
```

`--project-receipt` copies the receipt into `.orcha/receipts/` in your
project. The format is [RFC 0003](docs/spec/rfcs/0003-run-attestation-envelope.md), with
golden vectors in [docs/spec/test-vectors](docs/spec/test-vectors/).

## Also in this repository

- **[`orcha-sdk`](sdk/)** builds an agent in a few lines and verifies
  receipts. `uvx orcha-sdk init my-agent` makes one.
- **The runtime** runs one goal across MCP, A2A and computer-use agents, with a
  registry, a planner and an orchestrator. `./scripts/run-all.sh` starts it, and
  the [docs](https://metaorcha.ai/docs) cover each service.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) says how to build, test and send a change.
Report a vulnerability the way [SECURITY.md](SECURITY.md) says, not in a public
issue.
