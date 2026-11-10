# Contributing to the MetaBot app

Thanks for helping. This guide covers what every contributor needs, whatever the OS.
Then read the guide for yours:

- [CONTRIBUTING-linux.md](CONTRIBUTING-linux.md): Ubuntu, Debian, Fedora;
- [CONTRIBUTING-macos.md](CONTRIBUTING-macos.md): macOS on Apple Silicon;
- [TESTING.md](TESTING.md): every test suite, what it proves, and which CI job runs it.

The repository-wide rules in the root [CONTRIBUTING.md](../../CONTRIBUTING.md) apply too.

## What the app is

MetaBot is the coding-agent harness in `apps/metabot`: it runs an agent on a task and
signs a receipt for every run. The MetaBot app (this folder, package `metaorcha-app`,
command `metabot-app`) lets you run and watch those agents from a phone or a desktop
window. It is a small Python server, standard library only, plus one web page. Each
agent works in a terminal "desk" (a tmux session); the page shows the desks, the runs,
and their receipts, and checks each receipt itself.

The desktop window and the Android app are coming in the first preview.

## Map of `src/metabot_app`

| Path | What it does |
|---|---|
| `cli.py`, `__main__.py` | the `metabot-app` command (`serve`, `--version`) |
| `server.py` | the HTTP, SSE and WebSocket server: auth, routes, the terminal bridge |
| `mb.py` | the MetaBot side: desks, runs, accounts, receipts; the exact tmux argv |
| `mbq.py` | MetaBot queries, run under MetaBot's own Python; one JSON object out |
| `mbqw.py` | the client of a warm `mbq.py serve` reader, so reads stay fast |
| `catalog.py` | the command catalogue: every write a module offers, with validated params |
| `modules/*.json` | the module manifests (runs, accounts, plugins, workflows and more) |
| `history.py` | the append-only event log (SQLite; insert and select only) |
| `index.html` | the whole page: markup, style and script |
| `vendor/` | vendored browser code: the terminal, the receipt verifier (`mbverify.js`), fonts |
| `selftest/` | the verifier self-test page and its corpus, served read-only |

Tests live in `tests/`, with fake `tmux`, `metabot`, `orcha` and `systemctl` binaries in
`tests/fixtures/`. `tools/run_unit.py` runs the unit suite under the clean-testing rule
and `tools/devguard.py` checks for leftovers.

## The rules

- **Test first.** Write the test, watch it fail, then make it pass. A change without a
  test is not ready for review.
- **No secrets.** No tokens, keys or passwords in code, tests, fixtures or logs. CI runs
  gitleaks on every pull request.
- **No private paths.** No home directories, host names, addresses or names of people.
  `tests/test_private_strings.py` walks the whole app tree for them.
- **Destructive actions always stop.** Anything that deletes, overwrites or kills asks the
  person first, every time. Never add a setting that turns that off.
- **Receipts are never weakened.** A change never makes a receipt sign less, or the
  verifier accept more. The verifier suite runs the corpus the SDK generates; every case
  in it keeps its verdict.
- **Names.** Public text says "Metaorcha" for the project; the app is "MetaBot".

## Sign-off (DCO)

Every commit carries a `Signed-off-by:` line with your author email, which certifies the
[Developer Certificate of Origin](https://developercertificate.org/). `git commit -s` adds
it. CI's `dco` job checks every commit in the pull request. To sign off commits you have
already made on your branch:

```sh
git rebase --signoff main
```

## Commit style

Conventional commits, scoped to the app: `<type>(metabot-app): <what changed>`, with
`feat`, `fix`, `test`, `docs`, `ci` or `chore` as the type. One logical change per commit.

```sh
git commit -s -m "fix(metabot-app): keep the desk list in order after a rename"
```

## Running the tests

From `apps/metabot-app`:

```sh
make check
```

`make check` runs four targets, each of which runs alone too:

```sh
make unit
make ui
make verifier
make guard
```

To run one unit test file under the same rules:

```sh
python3 tools/run_unit.py -p "test_routes.py"
```

## Clean testing

`make unit` runs `tools/run_unit.py`, not bare `unittest`. It fails the run when:

- any test fails;
- any test leaves a process, socket or file open: the suite runs with
  `-W always::ResourceWarning`, and one ResourceWarning anywhere fails it;
- any test leaves something in its own temporary folder: the suite gets a private
  `TMPDIR`, which is removed afterwards only if it is empty, so a leak is never hidden.

A passing run ends with a line that starts `clean:`. The reason: a test that leaks still
passes alone, but it leaves processes and files on every contributor's machine and makes
other tests flaky. So tests use temporary folders, fake binaries and ports 8471 to 8479;
they never touch your real MetaBot home or your tmux server, and never start a real
agent CLI. `make guard` then checks that nothing is still listening on those ports and
that no tmux server runs on the test socket `mb-test`.

## What a reviewer runs

A reviewer checks out the branch and runs, from `apps/metabot-app`, the target for their
OS, which is exactly what CI runs:

```sh
make test-linux
```

or, on a Mac:

```sh
make test-macos
```

They then read the diff for the rules above, and check that every commit is signed off.
