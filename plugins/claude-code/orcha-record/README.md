# orcha-record — a signed receipt of your Claude Code session

Turn a coding session into a receipt of what ran: every tool call journaled
on your machine, sealed on Stop with a key you hold, verifiable offline with
the published package. Nothing is hosted and nothing leaves the machine.

## Sixty seconds

You need [uv](https://docs.astral.sh/uv/). Everything below runs offline
after the first `uvx` resolves the package.

**1. Verify the golden pair** (two receipts from the spec's worked example —
one intact, one with a step changed after sealing):

```bash
uvx --from 'orcha-sdk>=0.2.0' orcha verify examples/golden-valid.json      # Verdict: VALID
uvx --from 'orcha-sdk>=0.2.0' orcha verify examples/golden-tampered.json   # Verdict: INVALID
```

**2. Flip a byte** in `golden-valid.json` — change one character of a
`tool` name, or of `signature` — and verify again. The verifier names the
link that broke: a step edit fails `steps_root`, `merkle_root` and
`signature`; a signature edit fails `signature` alone.

**3. Mint your key** (an Ed25519 seed in `~/.orcha/key`, mode `0600`; it is
never printed):

```bash
uvx --from 'orcha-sdk>=0.2.0' orcha record keygen
```

**4. Install the plugin** in Claude Code — from the Metaorcha marketplace
once it is listed, or straight from a checkout:

```
/plugin marketplace add solvent-labs-org/metaorcha
/plugin install orcha-record
```

**5. Run a session.** Each tool call is appended to
`<cwd>/.orcha/runs/<session_id>.json`; when Claude stops, the journal is
sealed into `<cwd>/.orcha/receipts/<session_id>.json` and the hook prints
the receipt's digest and the verify line:

```bash
uvx --from 'orcha-sdk>=0.2.0' orcha verify .orcha/receipts/<session_id>.json
```

Raw arguments and outputs stay in the journal on your machine; the receipt
carries only their hashes.

## What the receipt proves — and what it does not

The receipt is **self-signed by your local key**. It proves who sealed it,
that nothing in it changed after sealing, and what each step committed to
(the tool, the hash of its arguments, the hash of its output, whether it
succeeded). It proves the run met what was declared — and a coding session
declares nothing by default, so it proves **what ran and nothing more**. It
does not prove the work was correct.

It commits to the tool calls in its steps and to nothing else: model calls
and planning are not steps, and a call that was absent from the journal is
absent from the receipt. `orcha verify` prints this coverage statement with
every verdict.

Nothing settles on a receipt from this plugin. Settlement and refusal stay on
the server path.

## Options

Set these in the environment Claude Code runs in.

| Variable | Effect |
| --- | --- |
| `ORCHA_RECORD_ARGS` | Extra arguments for the hook, e.g. `--criteria exit_zero` to declare that every `Bash` call must exit 0 (a nonzero exit then fails the run's `declared_acceptance` verdict, signed in the receipt). |
| `ORCHA_KEY_PATH` | Key file instead of `~/.orcha/key`. |
| `ORCHA_SDK_SPEC` | Package spec for `uvx --from` (default `orcha-sdk>=0.2.0`); a path to a checkout works. |

The hook never blocks a tool call or a Stop: it exits 0 (recorded or sealed)
or 1 (an error the harness shows you). Without a key file, sealing is
refused and the session continues; the journal is kept, so you can mint a
key and seal it on the next Stop.

## Layout

```
.claude-plugin/plugin.json   the plugin manifest
hooks/hooks.json             PostToolUse, PostToolUseFailure, Stop → hooks/record.sh
hooks/record.sh              uvx --from orcha-sdk orcha record hook claude-code
examples/golden-*.json       the spec's worked example, intact and tampered
```

The hook is a launcher and nothing else; the adapter is
`emerge.hooks` in the SDK, the envelope is RFC 0003
(`docs/spec/rfcs/0003-run-attestation-envelope.md`), and the verifier is
`emerge.run_attestation` — the same verifier the platform's settle gate
reads.
