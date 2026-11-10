---
name: monitor
description: 'Use the monitor connection to see and act on the PC screen: look first, then show a page or a card, focus, close, type or click.'
---

# monitor

The `monitor` connection is this PC's screen. Order of work:

1. `screen_look` first: it saves a screenshot and lists the windows. Read the
   titles before acting; never act on a window you have not seen listed.
2. To put something in front of the person, prefer `show_text` (a card and a
   notification) or `show_url` (a page or a local file in a new browser
   window) over typing into their windows.
3. `focus_window` by a title fragment, then `type_text`, `press_key` or
   `click` only when the task needs it. These need `xdotool` on the PC; when
   the tool says it is not installed, stop and say so.
4. `close_window` is destructive: the gate stops for it. Say which window and why.
5. End with one `screen_look` so the receipt shows the result.
