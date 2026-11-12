# Demos: MetaBot proving itself

The marketplace under `plugins/` and the scripts under `tools/demo/` turn one
machine into a running proof: a watchdog loop that reads the app's own state
every five minutes, a Cursor run at a chosen model tier, a workflow that chains
the two, a custom sub-harness on a local model, the PC screen as a tool, and
the browser as a connection. Every one of them ends in a signed receipt, and
every receipt is readable from the paired phone under Runs & Receipts.

## Set up

```sh
cd <the folder the runs should work in>
tools/demo/setup.sh           # plugins, the watchdog routine, the two workflows
metabot workflow approve inception    # an approval is yours, at a terminal
metabot workflow approve tiers
metabot serve                 # or Start serve in the app: the loop fires every 5 minutes
```

`METABOT` names the `metabot` to use; `DEMO_FOLDER` the folder; `DEMO_CURSOR`
the Cursor model family the tiers map to (`grok-4.7`); `DEMO_LOCAL_MODEL` the
local model (`qwen2.5:3b`).

## What each piece is

| Piece | Kind | Proof it leaves |
|---|---|---|
| `watchdog` plugin | a bridge with no model: probes `/api/meta`, devices, desks, runs, accounts, doctor, the server process, disk and load | one receipt per firing, seven steps, `WATCHDOG OK` or `ATTENTION (n)` with the findings first |
| `watchdog` routine | `*/5 * * * *` on that bridge; fires under `metabot serve` | `metabot routine firings watchdog`, or the Runs tab on the phone |
| `tools/demo/run.sh TIER task` | one Cursor run at `low`, `medium`, `high` or `xhigh` (`grok-4.7-TIER`), read-only unless `--write`; `--engine local` runs the sub-harness | the receipt names the model that ran |
| `tiers` workflow | the same question at three Cursor tiers | one chain of three receipts, each with its model and cost |
| `inception` workflow | the watchdog, then Cursor with the `monitor` connection shows a status card on the PC screen | two receipts in one chain; the card on the monitor |
| `monitor` plugin | an MCP server for the PC screen: look, windows, show a page or a card, focus, close, type and click | each call a gated step in the receipt; `close_window` always stops |
| `local-harness` plugin | a developer's own agent loop on a local OpenAI-compatible model, run as an engine | the receipt shows its tool calls and tokens like any engine's |
| `browser` plugin | the skill for the browser connection and `browser-connect`, which adds it (Playwright MCP, isolated Chromium) | each call classed by the origin it acts on |
| `tools/demo/new-harness.sh NAME` | scaffolds your own bridge plugin | `metabot ask -e NAME` once enabled |

## Computer use

`monitor` is the computer-use surface: `screen_look` saves a screenshot and
lists the windows, `show_text` and `show_url` put something in front of the
person, `focus_window`, `type_text`, `press_key`, `click` and `mouse_move` act.
It needs `scrot`, `wmctrl` and a browser on PATH; typing and clicking need
`xdotool`, and the tool says so when it is missing. The gate classes every
call: reads run, writes ask unless allowed (`--allow monitor-monitor__show_text`),
`close_window` is destructive and always stops.

Known limits on the engines, as of 2026-11-11:

- `cursor-agent` in headless mode rejects an MCP tool call it has not been told
  to force (`User rejected MCP`), so a Cursor run cannot yet call the monitor
  through the switchboard; the core's Cursor engine needs to pass Cursor's
  force flag for switchboard calls (a core item).
- Small local models often write a tool call as text instead of calling it;
  the core's text-call fix handles that once it is in the release.

The plugin itself is proven directly: `metabot connect test monitor-monitor`
lists its ten tools, and the test suite drives every tool with fakes on PATH.

## Where the receipts are

`metabot receipts list`, or the app's Runs & Receipts (the phone too). A
firing of the loop is a run like any other; `interrupted` marks one the app
lost mid-run.
