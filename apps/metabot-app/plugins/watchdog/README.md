# watchdog

A MetaBot bridge with no model. Each run probes the app server (`/api/meta`,
devices, desks, runs, accounts, doctor) and the machine (the server process,
disk, load), one tool step per probe, and answers `WATCHDOG OK` or
`WATCHDOG ATTENTION (n)` with the findings first. A routine on it is a watchdog
loop; its firings are signed receipts you can read from the phone.

```sh
metabot plugin marketplace add metabot-demos <this folder's parent>
metabot plugin install watchdog@metabot-demos
metabot plugin enable watchdog --allow-bridges
metabot ask -e watchdog --read-only "watch"
metabot routine save watchdog --cron '*/5 * * * *' -e watchdog "watch"
metabot serve    # or Start serve in the app
```

It finds the app through `METACHAT_STATE`, else the `state_dir` of
`METACHAT_CONFIG` or `~/.config/metachat/config.json`. The token is read from
that folder and never printed. `WATCHDOG_DISK_FLOOR_GB` (4) and
`WATCHDOG_DEVICE_QUIET_S` (21600) tune two findings. "json" in the prompt gets
the report as JSON.
