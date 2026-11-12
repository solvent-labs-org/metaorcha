# metabot-demos: a marketplace of dogfood plugins

Four plugins that prove MetaBot on itself. Each is installed by hash from this
folder and enabled with the permission it needs; nothing here is on by default.

| Plugin | What it is | Enable with |
|---|---|---|
| `watchdog` | a bridge (`metabot.bridge/1`) with no model: it reads the app server's state and reports; a routine on it is a watchdog loop | `--allow-bridges` |
| `monitor` | an MCP server for this PC's screen: look, list windows, show a page or a card, focus, close, type and click (xdotool) | `--add-servers` |
| `local-harness` | a custom sub-harness: a local OpenAI-compatible model (Ollama) with its own tool loop, run as an engine | `--allow-bridges` |
| `browser` | a skill for the browser connection and `bin/browser-connect`, which adds it (Playwright MCP, isolated Chromium) | plain |

The scripts under `tools/demo/` install them, save the loops and workflows, and
run them at a chosen Cursor model tier. See `docs/DEMOS.md`.

Rules the plugins keep: no secret in an argument or on stdout (the watchdog
reads the app token from the state folder and never prints it); a tool that
closes or deletes is classed destructive by its name; a read-only job is
honoured by both bridges; every run ends in a signed receipt like any other.
