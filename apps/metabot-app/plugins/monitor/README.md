# monitor

An MCP server (standard library, stdio) for the screen of the PC MetaBot runs
on. Enable it as a gated connection:

```sh
metabot plugin install monitor@metabot-demos
metabot plugin enable monitor --add-servers
metabot connect list      # the connection the plugin added
```

Tools: `screen_look`, `windows` (read); `show_url`, `show_text`,
`focus_window`, `type_text`, `press_key`, `click`, `mouse_move` (write, the gate
asks unless allowed); `close_window` (destructive, always stops). Needs `scrot`,
`wmctrl` and a browser (`google-chrome`; `MONITOR_BROWSER` overrides) on PATH;
typing and clicking need `xdotool`. `DISPLAY` defaults to `:0`.
