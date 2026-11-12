# The desktop shell

A window around the app, for Linux first (macOS in December). It holds no logic
of its own: it finds the app server this person already runs, or starts
`metabot-app serve` from PATH, and opens the page with the desktop token.

| Part | What it is | Proof |
|---|---|---|
| `desktop/shell-core` | a standard-library Rust crate: the state folder the way the server finds it (`METACHAT_STATE`, the config's `state_dir`, `~/.local/state/metachat`), the live `server.json` (pid checked), the token file, the page URL, the server on PATH, the plan (attach, start, missing), waiting for a started server | `cargo test` (6 tests) |
| `desktop/src-tauri` | the Tauri 2 crate: one window, `main`, on the page URL, or on `ui/missing.html` when no server can be found or started; `METABOT_SHELL_SMOKE=1` prints the plan and port and exits, so CI proves the binary without a display | `tools/desktop-smoke.sh`, `tests/test_desktop_shell.py` |
| CI `bundle-linux` | `make desktop-test`, then `cargo tauri build --bundles deb,appimage`, the smoke under xvfb, the bundles as artifacts; on dispatch or the `bundle-ci` label | the job's log |

What the window never does: it holds no API of its own, no settings, no
updater; the token never reaches a log (the smoke prints the port alone); a
server it starts is detached, so closing the window keeps it running for the
browser and the phone.

Install the bundle, then open "MetaBot" from the launcher. With no
`metabot-app` on PATH it shows the install page: `uv tool install metaorcha-app`.
