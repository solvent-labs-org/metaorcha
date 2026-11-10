# Contributing to the MetaBot app on Linux

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: it has the rules, sign-off and commit style.

## Supported

- Ubuntu 22.04 and 24.04
- Debian 12
- Fedora 40

Other distributions may work; issues and fixes for them are welcome.

## Prerequisites

- Python 3.11 or newer (uv installs one if your system's is older);
- tmux 3.2 or newer;
- Node 20;
- uv, git and make.

Ubuntu 22.04 and 24.04, Debian 12:

```sh
sudo apt-get update
sudo apt-get install -y git make curl tmux
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Fedora 40 (its `nodejs` package is Node 20):

```sh
sudo dnf install -y git make tmux nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Check the versions:

```sh
tmux -V
node --version
uv --version
```

## Setup

Clone the repository and make one virtual environment at its root, with this tree's
MetaBot, SDK and app in it. The real-MetaBot tests in `tests/test_extend.py` look for it at
`.venv/bin/python` and skip without it. Activating it also puts a new enough `python3`
on your PATH, which the test fixtures need.

```sh
git clone https://github.com/solvent-labs-org/metaorcha.git
cd metaorcha
uv venv --no-project --python 3.12 .venv
uv pip install --python .venv/bin/python -e ./sdk -e ./apps/metabot -e ./apps/metabot-app
source .venv/bin/activate
cd apps/metabot-app
```

If you already ran the repository's `make install`, its `.venv` has MetaBot and the SDK:
add the app with `uv pip install --python .venv/bin/python -e ./apps/metabot-app`, then
activate it.

## Run the tests

CI's `linux` job runs exactly `make -C apps/metabot-app test-linux`. From `apps/metabot-app`
that is:

```sh
make test-linux
```

It runs the unit suite under the clean-testing rule, the page's smoke test, the receipt
verifier suite and the leftover guard. [TESTING.md](TESTING.md) says what each proves.
A passing unit run ends with a line that starts `clean:`.

The fresh-user harness, which installs and starts the app in an empty home folder, and
the desktop bundle are coming in the first preview.

## Known issues

- **Blank or flickering desktop window (WebKitGTK).** On some GPU drivers, WebKitGTK's
  DMA-BUF renderer draws nothing. Start the desktop window with
  `WEBKIT_DISABLE_DMABUF_RENDERER=1` set. This applies to the desktop window, coming in
  the first preview.
- **Desks that die when the service restarts.** If you run the app under a systemd user
  unit, set `KillMode=process` in it. The default, `control-group`, kills every process
  the service started, and the tmux server that holds your desks is one of them.
- **A cursor run refuses to start without bubblewrap.** MetaBot hides the instruction
  files outside the project from an agent with `bwrap`. The cursor engine reads every
  rule folder above its project, so without a working `bwrap` its runs refuse. Install
  bubblewrap (`sudo apt-get install -y bubblewrap` or `sudo dnf install -y bubblewrap`),
  or turn the sandbox off knowingly with `metabot config set sandbox false`; the receipt
  then records how many sources stayed in view.

## Filing a bug

Use the "MetaBot app on Linux" issue form. It asks for:

- your distribution and version, and `uname -srm`;
- the output of `metabot-app --version`, `python3 --version`, `tmux -V` and `node --version`;
- the steps, what you expected, and what happened;
- logs, with tokens, keys and anything private removed.

A `metabot-app doctor --json` report, which has no secrets by design, is coming in the
first preview.
