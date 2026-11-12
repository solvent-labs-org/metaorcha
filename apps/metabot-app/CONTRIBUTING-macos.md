# Contributing to the MetaBot app on macOS

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: it has the rules, sign-off and commit style.

## Supported

- macOS 14 (Sonoma) and macOS 15 (Sequoia) on Apple Silicon.
- Intel Macs are best effort: issues and fixes are welcome, but CI runs on Apple Silicon only.

## Prerequisites

- Xcode Command Line Tools (for git and make);
- Homebrew;
- Python 3.11 or newer (the system `python3` is older; uv installs one);
- tmux 3.2 or newer, from Homebrew;
- Node 20, from Homebrew;
- uv.

```sh
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install tmux node@20 uv
echo 'export PATH="$(brew --prefix node@20)/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

`node@20` is keg-only, which is why its folder goes on your PATH. Check the versions:

```sh
tmux -V
node --version
uv --version
```

## Setup

Clone the repository and make one virtual environment at its root, with this tree's
MetaBot, SDK and app in it. The real-MetaBot tests in `tests/test_extend.py` look for it
at `.venv/bin/python` and skip without it. Activating it also puts a new enough
`python3` on your PATH, ahead of the system one, which the test fixtures need.

```sh
git clone https://github.com/solvent-labs-org/metaorcha.git
cd metaorcha
uv venv --no-project --python 3.12 .venv
uv pip install --python .venv/bin/python -e ./sdk -e ./apps/metabot -e ./apps/metabot-app
source .venv/bin/activate
cd apps/metabot-app
```

## Run the tests

CI's `macos` job runs exactly `make -C apps/metabot-app test-macos`. From `apps/metabot-app`
that is:

```sh
make test-macos
```

It runs the same suites as Linux: the unit suite under the clean-testing rule, the
page's smoke test, the receipt verifier suite and the leftover guard. Nothing in it is
Linux-only. [TESTING.md](TESTING.md) says what each proves. A passing unit run ends with
a line that starts `clean:`.

macOS runner minutes are limited, so the `macos` job runs only when a maintainer starts
it by hand (a dispatch with the `macos` input on) or adds the `macos-ci` label to a pull
request. Run `make test-macos` yourself and tick macOS in the pull request's "Tested on"
list. The suite does not pass on macOS yet: its first run (2026-11-11) failed on paths
under `/private` and on timing; the port is a December item.

The fresh-user harness and the desktop bundle are coming in the first preview.

## Known issues

- **Gatekeeper blocks the app on first launch.** Preview builds are not notarized yet.
  macOS 15 removed the Control-click "Open" bypass: open the app once, then go to System
  Settings, Privacy & Security, choose "Open Anyway" for it, and confirm. On macOS 14,
  Control-click the app and choose Open also works.
- **App Translocation and quarantine.** An app opened straight from a download, or from
  the disk image, runs from a random read-only copy and cannot find its own files. Drag
  it to Applications first and open it from there.
- **Finder's bare PATH.** An app started from Finder or the Dock gets only
  `/usr/bin:/bin:/usr/sbin:/sbin` on its PATH, not your shell's, so it does not see
  Homebrew's `tmux` or your agent CLIs. While developing, start the server from Terminal
  with `metabot-app serve`, where your shell's PATH applies.
- **A cursor run refuses to start.** MetaBot hides the instruction files outside the
  project from an agent with bubblewrap (`bwrap`), which is Linux-only. The cursor engine
  reads every rule folder above its project, so without `bwrap` its runs refuse. On a Mac,
  turn the sandbox off knowingly with `metabot config set sandbox false`; the receipt then
  records how many sources stayed in view.

## Filing a bug

Use the "MetaBot app on macOS" issue form. It asks for:

- your macOS version and chip, from `sw_vers` and `uname -m`;
- the output of `metabot-app --version`, `python3 --version`, `tmux -V` and `node --version`;
- the steps, what you expected, and what happened;
- logs, with tokens, keys and anything private removed.

A `metabot-app doctor --json` report, which has no secrets by design, is coming in the
first preview.
