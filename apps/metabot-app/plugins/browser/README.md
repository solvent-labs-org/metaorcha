# browser

The browser MetaBot drives is Playwright's own Chromium (`@playwright/mcp`,
`--browser chromium --isolated`), never the person's browser. This plugin is
the skill that says how to use it and a script that adds the connection:

```sh
metabot plugin install browser@metabot-demos
metabot plugin enable browser
plugins/browser/bin/browser-connect --origin https://example.com
metabot connect list
```

`browser-connect --mcp /path/to/node_modules/@playwright/mcp/cli.js` uses a
copy already installed; without it `npx` fetches the package on first use.
`--headed` shows the browser on the monitor. The gate classes each call by the
origin it acts on; `--origin` lists where it may act besides this machine.
