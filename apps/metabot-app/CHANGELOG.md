# Changelog

The app's releases, newest first. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
A change to a contract (the receipt, the workflow file, the bridge protocol, the app API,
the pairing payload, the capability block, the module manifest) is called out as such.

## [Unreleased]

### Added
- Mail sent to the person (`metabot mail send tablet --from <desk> -m ...`) lands in that
  desk's lane on the page, read once; the hand-off hint names this command when no inbox
  adapter is configured (`mc-send` is the adapter's command, not part of the wheel).
- Every desk the app spawns carries `METABOT_MAILBOX=<desk name>` in its environment, so a
  per-home `metabot mail hook` (Claude or Cursor) brings that desk its mail without naming it.
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
  `setup_first_run`, `ask_receipt`, `tools_offline`, `workflow_chain`, `concurrency`, `recovery_kill`,
  `recovery_engine`, `wrong_key`, `receipt_view_clarity`,
  the report, `tools/selftest-linux.sh` and the tablet walk `tools/tablet-e2e.sh`.
- The claims page (`docs/CLAIMS.md`) and the test that keeps every claim tied to a proof.
- `POST /api/run` takes `model` (the engine's model name, passed to the core as `-m`); the run
  sheet has a model field, so a Cursor run picks its tier per run.
- The demo marketplace `plugins/` (`watchdog`, `monitor`, `local-harness`, `browser`) and the
  scripts `tools/demo/` that install them, save the watchdog loop, add the workflows and run
  Cursor at a tier (`docs/DEMOS.md`).
- The empty lane opens with "What should we get started with?".
- `POST /api/run` applies a Cursor account (`account`) the way a Cursor desk does (its config
  home), so a run picks its Cursor sign-in per run; an unknown account is a 404 for every engine.
- The desktop shell for Linux (`desktop/`): a Tauri 2 window that finds or starts the app server
  and opens its page; CI's `bundle-linux` job builds the `.deb` and AppImage (`docs/DESKTOP.md`).
- Runs without tmux (T2/E3): on a host with no tmux, and on the phone, `POST /api/run` starts the
  core as the app's own child (the prompt on stdin, stdout and stderr into `STATE/runs/<name>.log`,
  the same scrubbed environment as a desk, one process group); the run links to its receipt, reports
  how it ended, counts toward `max_runs`, and `POST /api/kill` ends the whole tree. New route
  `GET /api/mb/runlog/<name>` (contract: app API) returns the log tail, pid, liveness and exit.
- A run a hosted engine refused (it answered 401 or 403) tells its lane to check the engine's key in
  Settings → Accounts, beside the engine's own error.
- A run whose process is gone and that sealed no receipt (the app, and the run with it, killed
  mid-run) is listed by `GET /api/mb/runs` as `state: "interrupted"` with a plain `problem`, never
  shown live or done and never dropped (contract: a third run state beside `live` and `done`).
- `make selftest-linux` runs the journeys twice: with tmux on PATH (desks) and as the phone
  runs them (`METABOT_ON_DEVICE=1`, no desks); `ask_receipt`, `tools_offline` and
  `recovery_engine` no longer need desks.
- The wheelhouse (`packaging/wheelhouse.py`): the sdk, the core and the app built as wheels,
  their pinned dependencies downloaded as wheels only (never an sdist) for one platform, and a
  `requirements.lock` with a sha256 per file, so a desktop bundle installs offline.

### Changed

- Page polish: the activity rail's labels wrap onto two lines instead of cutting a word, and the
  new-account name field keeps its width when the row reflows.
- The run sheet's engine picker follows the capability block: it offers the engines this computer
  has and defaults to the first of them, never a hard-coded engine.
- A Cursor run picks its account in the run sheet (posted as `account`); an unknown account is a
  plain "pick one from the list", never a run.
- The run sheet's model chip offers the engine's own models as a datalist; cursor shows its four
  tiers.
- A run whose state is `interrupted` has its own glyph and word ("interrupted (no receipt)"), the
  problem rides its rows, and its record says what happened instead of checking for receipt bytes
  that are not there.
- A run desk's shell prompt after its run has ended is an idle shell, not an ask: the waiting row
  and the prompt sheet say the run ended, and no one-tap allow keys are offered for it.
- An unapproved workflow row carries an Approve button that opens the approve desk.
- Sign-in desks sit in their own "Signing in" group in the tree, never under a folder path.
- The page gates its surfaces on the capability block (1.3): without tmux, no desk-starting card, no desk
  set-up step, a spawn that explains, and a run record that opens its run log instead of a desk; cursor
  is offered as unavailable without a sandbox; sign-in offers the cli engines this computer has; the
  keep-running line names the host's service.
- A phone the computer removed is told once, then the page hands itself to the APK's pairing screen
  through `metabot://retry`.
- Settings → Phone: pair a phone (code in groups of four, the four words, a countdown), the paired
  devices with Remove; the phone scope shows its own name; on-device the section points at another
  computer. The QR lands with the vendored encoder.
- Set-up step 0: the doctor report — a failing row shows its fix, a blocker row is red and opens the
  set-up page by itself; the folder step picks a real folder (`POST /api/folders`) where the route exists.
- Settings → Developer shows the self-test button, disabled until the on-device runner lands.
- The prompt sheet and the waiting row take a `text` ask as words: a line and a Send that posts to
  `/api/send` (keys alone cannot answer it).
- A lane event from a desk to itself (`via: "desk"`, `target == from`) reads as the desk speaking
  to you, never "desk → desk".
- The prompt sheet says "answered — the desk moved on" the moment its desk stops asking, its keys
  go with it, and a new ask brings them back.
- On a phone app the Settings page has no Phone section; a desktop whose capability block says no
  phone listener shows one line in its place.
- The sheet's key buttons carry `touch-action: manipulation` (the 44 px target was already there);
  the 11-13 tablet day checks them on hardware.

### Security
- A wrong token or pairing code locks the address out briefly, per listener and per kind;
  a successful pairing clears the lockout.
- The page serves only what the host's capabilities allow; the key and login folders are
  never listed or chosen as a project.

### Fixed
- A free-text prompt in a desk (Cursor's "Tell the agent what to do instead") is the `text`
  ask, and a chat message answers it instead of being refused as "the desk is waiting".
- A finished run desk no longer reads as asking: between the run script's exit line and
  the shell's first prompt the model's answer could quote an ask and open the prompt sheet.
