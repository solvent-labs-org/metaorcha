# The self-test

One runner, the same journeys on Linux and on the phone, against a scripted
fake model server, with every receipt verified on the machine that made it.
It is the blackout test: nothing it does needs a network.

```sh
METABOT_PYTHON=/path/to/venv/bin/python make -C apps/metabot-app selftest-linux
# or a subset, with budgets enforced:
METABOT_PYTHON=... make -C apps/metabot-app selftest-linux SELFTEST_ARGS="--journeys ask_receipt --enforce-budgets"
```

`METABOT_PYTHON` is an interpreter with the MetaBot core installed; its
`metabot` and `orcha` commands sit beside it. The wrapper runs the runner in a
user network namespace with only loopback when the machine allows one, and
says so when it does not. The session folder lands under
`/var/tmp/metabot-app/selftest/` (or `SELFTEST_OUT`): `report.jsonl`,
`report.md`, `logs/`, and with `--keep` the whole scratch world.

## What a run is

The runner (`metabot_app.selftest.run`) builds a scratch world: a MetaBot home
with **its own signing key** (`ORCHA_KEY_PATH`), an app state folder, a job
folder that is the only project root, and a user home of its own. It starts the
fake engine, runs `metabot init`, starts the app server as a child process on
loopback, and runs the journeys in the plan's order. Each journey is a module
under `selftest/journeys/`: an axis, the caps it needs (missing → the journey is
**skipped with the reason**, never silent), its step budgets, and the scenarios
it installs.

The fake engine (`metabot_app.selftest.fake_engine`) is an OpenAI-compatible
chat server that answers from a scenario: one JSON line per model turn
(`answer`, `tool_call`, `refuse`, `error`, `truncate`, `delay_ms`). The
request's `model` names the scenario and the turn is the number of assistant
messages already in the conversation, so one engine serves concurrent runs.

## The report

`report.jsonl` has one line per step, written as the step ends, and a summary
line last, so a run the server died under keeps every step it took. A step is
`pass`, `fail`, `skip` or `invalid` (the page took it while hidden). Budgets are
recorded on every run and enforced only with `--enforce-budgets`, which the
tablet runs use. `tools/selftest_report.py REPORT.jsonl` renders the table and
exits 0 clean, 1 a failed step, 2 a budget miss, 3 incomplete.

## Journeys today

| journey | axis | proves |
|---|---|---|
| `cold_start` | speed | server start to the API answering, first and warm |
| `setup_first_run` | setup | the key was made for this world, the core's `doctor` reads clean, `GET /api/doctor` has no blocker and sees the local model server, the caps are reported |
| `ask_receipt` | clarity | a run through the app, verified by the app's route and by the core |
| `tools_offline` | real work | write, read and shell through the core's gate; the receipt names every tool; a read-only run through the app |
| `workflow_chain` | real work | a four-step `metabot.workflow/1` (two asks, a ladder, an ask) across two providers (the OpenAI-compatible fake and a `metabot.bridge/1` bridge) and two accounts (the ladder's first rung, account alpha, answers 500; the second, account beta, answers); approved at a terminal, refused when piped; every receipt names its engine, endpoint and rung; the exported bundle verifies offline as one chain; a rewritten rung is caught; a missing account stops the chain and still leaves receipts that verify |
| `recovery_engine` | recovery | a failing, refusing, truncating or slow model server: every run ends, says why, and its receipt verifies and records the failure |

A run whose model server fails still gets a signed receipt that verifies, with
`ok` false and the error in it; that is the clarity the journey checks for.

## The tablet walk (`tools/tablet-e2e.sh`)

`tools/tablet-e2e.sh remote_pair` repeats the phone pairing over a USB cable with no touch input, so a pairing change is proven on a real device before it ships. It needs the app server running on this computer with its phone listener on, a **debuggable** APK (`android/build.sh` with `MB_DEBUGGABLE=1`), the tablet attached with USB debugging on, and nobody using the tablet. Nothing leaves the cable: the tablet reaches the TLS listener on its own loopback through `adb reverse`.

```bash
MB_STATE=/path/to/state MB_CONFIG=/path/to/config.json tools/tablet-e2e.sh remote_pair [--apk PATH] [--port 8471] [--tls 8473]
```

Steps, each a line in the same `report.jsonl` shape as the Linux runner: install (a build signed with another key is uninstalled first; only this test package), the reverse tunnel, a pairing code from `metabot-app pair`, the words the tablet logged compared with the computer's, the page open over TLS, the device listed, revoke from the CLI, the pairing screen back, and a second pairing. Screenshots and `report.md` sit beside the report under `TABLET_OUT` (default `/var/tmp/metabot-app/tablet/<timestamp>/`).

The hook the script uses exists only in a debuggable build: the launcher activity forwards the intent extras `address`, `code` and `confirm` to the pairing screen, which types them once the page has loaded and logs the four words. A release build ignores the extras, and the pairing screen itself is never exported.

## Two passes on Linux

`make selftest-linux` runs every journey twice: once with tmux on PATH (`caps.desks` true,
a run is a desk) and once under `METABOT_ON_DEVICE=1` (no desks, no sandbox, the app runs the
core as its own child, as the phone does). Each pass writes its own session folder under
`SELFTEST_OUT/desks` and `SELFTEST_OUT/on-device`; CI archives both reports.
