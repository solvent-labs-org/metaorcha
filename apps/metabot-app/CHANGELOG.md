# Changelog

The app's releases, newest first. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
A change to a contract (the receipt, the workflow file, the bridge protocol, the app API,
the pairing payload, the capability block, the module manifest) is called out as such.

## [Unreleased]

### Added
- The app server (`metabot-app serve`): the page, `/api/*`, the event stream and the
  terminal WebSocket, behind a per-device token.
- First run: `metabot-app doctor [--json]` and `GET /api/doctor`; folders (`GET /api/dirs`,
  `POST /api/folders`); accounts and sign-in desks; the capability block in `GET /api/meta`
  (contract: `caps`).
- Phone pairing: a self-signed certificate per host, a TLS listener, a pairing code and QR
  (contract: `metabot-pair:1?a=…&p=…&c=…&f=…`), device tokens, revoke that closes live
  connections, `metabot-app pair` and `metabot-app devices`.
- A generic Android app (remote mode: pair, then the page over TLS, pinned to the
  computer's certificate).
- Keep running: `metabot-app service install|remove|status` (a systemd user unit on Linux,
  a launchd agent on macOS; no root).
- One server per state folder (`STATE/server.json`); a second `serve` reports the first.
- The self-test: a scripted fake engine, a runner, the journeys `cold_start`,
  `setup_first_run`, `ask_receipt`, `tools_offline`, `workflow_chain`, `recovery_engine`,
  the report, `tools/selftest-linux.sh` and the tablet walk `tools/tablet-e2e.sh`.
- The claims page (`docs/CLAIMS.md`) and the test that keeps every claim tied to a proof.
- Runs without tmux (T2/E3): on a host with no tmux, and on the phone, `POST /api/run` starts the
  core as the app's own child (the prompt on stdin, stdout and stderr into `STATE/runs/<name>.log`,
  the same scrubbed environment as a desk, one process group); the run links to its receipt, reports
  how it ended, counts toward `max_runs`, and `POST /api/kill` ends the whole tree. New route
  `GET /api/mb/runlog/<name>` (contract: app API) returns the log tail, pid, liveness and exit.
- `make selftest-linux` runs the journeys twice: with tmux on PATH (desks) and as the phone
  runs them (`METABOT_ON_DEVICE=1`, no desks); `ask_receipt`, `tools_offline` and
  `recovery_engine` no longer need desks.
- The wheelhouse (`packaging/wheelhouse.py`): the sdk, the core and the app built as wheels,
  their pinned dependencies downloaded as wheels only (never an sdist) for one platform, and a
  `requirements.lock` with a sha256 per file, so a desktop bundle installs offline.

### Security
- A wrong token or pairing code locks the address out briefly, per listener and per kind;
  a successful pairing clears the lockout.
- The page serves only what the host's capabilities allow; the key and login folders are
  never listed or chosen as a project.
