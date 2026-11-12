---
name: browser
description: 'Use the browser connection (Playwright MCP) to open pages, read them and act on them, within the origins the connection allows.'
---

# browser

The `browser` connection drives an isolated Chromium of its own (never the
person's browser or profiles). The gate classes each call by the origin it acts
on: this machine is always allowed; other origins only when the connection
lists them.

1. `browser_navigate` to the page, then `browser_snapshot`: the snapshot is the
   accessibility tree; act on the elements it names, never on guesses.
2. Read with the snapshot; `browser_take_screenshot` only when a picture is
   what the person asked for.
3. `browser_click`, `browser_type`, `browser_select_option` on named elements;
   snapshot again after each action that changes the page.
4. Never enter a password or a code; stop and say the page asks for one.
5. A page outside the allowed origins is refused by the gate: say so, do not
   look for another way in.
