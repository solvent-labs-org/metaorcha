# Testing the MetaBot app

Every suite runs from a Makefile target in `apps/metabot-app`, and CI calls only those
targets. Times are for a recent laptop; CI runners are slower.

| Target | What it runs | Time | CI job |
|---|---|---|---|
| `make unit` | the Python unit suite, under the clean-testing rule | about 5 min | `linux`, `macos` |
| `make ui` | the page's smoke test: static checks, then the script under a fake DOM | about 15 s | `linux`, `macos` |
| `make verifier` | the in-page receipt verifier against the SDK's corpus | about 2 s | `linux`, `macos` |
| `make guard` | nothing is left on the test ports or the test tmux socket | under 1 s | `linux`, `macos` |
| `make check` | all four above | about 5 min | (through the two below) |
| `make test-linux` | `check` plus `guard`: exactly the `linux` job | about 5 min | `linux` |
| `make test-macos` | `check` plus `guard`: exactly the `macos` job | about 5 min | `macos` |

The `dco` job runs no make target: it runs `.github/scripts/check_dco.py` over the pull
request's commits. Its own tests are in the unit suite (`tests/test_dco.py`).

```sh
make check
```

## No real agents, no real state

No test starts a real agent CLI, touches your MetaBot home, or talks to your tmux server.
The suites run against fakes in `tests/fixtures/`: `fake-tmux`, `fake-metabot`,
`fake-orcha`, `fake-mbpython` and `fake-systemctl`, which log what they were asked and
answer with fixed output. Each test gets a temporary home, state folder and MetaBot home
(`tests/fixtures/harness.py`). The few tests that need the real `tmux` binary use a
private socket and skip without it. Test servers bind to `127.0.0.1` on a free port, or
on ports 8471 to 8479.

This is why a test can say exactly which `tmux` argv the app built, and why the suite is
safe to run on the machine you work on.

## `make unit`: the Python suite

`tools/run_unit.py` runs `unittest discover -s tests` with a private `TMPDIR` and
`-W always::ResourceWarning`. It fails on any failed test, any leaked process, socket or
file, and anything left in the temp folder. A passing run ends with `clean:`. See
[CONTRIBUTING.md](CONTRIBUTING.md#clean-testing) for why.

What the files prove, by area:

| Area | Files |
|---|---|
| Auth: host check, token, CSRF header, body limits, cookie flags; traversal ids refused | `test_auth.py`, `test_ids.py` |
| Config loading, defaults; the env rules equal MetaBot's own (diffed against its source) | `test_config.py`, `test_env_rules.py` |
| Routes, static files, the terminal WebSocket and its close codes | `test_routes.py`, `test_static.py`, `test_ws.py` |
| Desks: spawn, run, kill with the exact tmux argv; live runs resolved to desks; resume | `test_spawn.py`, `test_runs.py`, `test_resume.py`, `test_run_report.py` |
| What the person sees: prompts waiting for them, sign-in, chat replies, the chat inbox seam | `test_ask_wrap.py`, `test_signin.py`, `test_mirror.py`, `test_inbox.py`, `test_hotfix.py` |
| The command catalogue and module manifests; the Extend page against real MetaBot | `test_catalog_modules.py`, `test_extend.py` |
| Server flows end to end: a workflow from new to a receipt that verifies | `test_flows.py` |
| Hardening and concurrency; review findings | `test_hardening.py`, `test_review_1_2.py`, `test_review_1_2b.py` |
| The append-only history log | `test_history.py` |
| The warm MetaBot reader: one request at a time, timeouts, restarts | `test_mbq_worker.py` |
| Clean teardown: no reader process or socket outlives a stopped app | `test_clean_teardown.py` |
| Packaging: one wheel with the page, vendored code and modules, and a `metabot-app` command | `test_package.py` |
| Nothing private in the shipped tree | `test_private_strings.py` |
| The dev guard itself | `test_devguard.py` |
| The guides and CI agree: make targets exist, jobs run what the guides say, version floors, SHA pins | `test_contributing.py` |
| The DCO check, against a throwaway git repository | `test_dco.py` |

Some tests skip when what they need is absent: the real-MetaBot tests in `test_extend.py`
need a Python with this tree's MetaBot installed (`METABOT_PYTHON`, by default the
repository's `.venv/bin/python`); the wheel tests need `uv`. CI provides both, so they
run there. A skip is not a pass: read the skip lines.

To run one file under the same rules:

```sh
python3 tools/run_unit.py -p "test_dco.py"
```

## `make ui`: the page

`tests/ui-smoke.mjs` reads `index.html` without a browser and checks what a static read
can prove: the element ids the page relies on, that its script parses, that every POST
carries the CSRF header, no `innerHTML` built from a variable, no outside URLs, the
contrast and tap-target rules, and the copy rules. It then runs `tests/ui-smoke-dom.mjs`,
which executes the page's script under a small fake DOM against the API's JSON shapes.

## `make verifier`: receipts

`tests/mbverify.test.mjs` runs the page's receipt verifier
(`src/metabot_app/vendor/mbverify.js`) over the corpus in
`src/metabot_app/selftest/fixtures/parity/cases.json`, plus the golden receipts. Every expected verdict in that corpus was computed by
the SDK (`tests/parity.py` generates it), so the in-page verifier and the SDK must agree
on every case, including the tampered ones. This is the suite that keeps receipts from
being weakened.

## `make guard`: leftovers

`tools/devguard.py --procs` fails if anything still listens on ports 8471 to 8479 or a
tmux server still runs on the test socket `mb-test`.

## Coming in the first preview

The fresh-user harness (install and start the app in an empty home folder), the desktop
bundles and the Android app, each with its own CI job.
