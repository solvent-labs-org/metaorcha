# MetaBot, the app

Run and watch your coding agents from a phone or a desktop window, with a signed receipt for every run.

The install guide lands with the first preview release.

## The commands

```
metabot-app serve                          start the server (the page, the API, the phone listener)
metabot-app doctor [--json]                what this machine has for a first run; exit 1 on a blocker
metabot-app pair [--port N]                show a pairing code and QR for a phone
metabot-app devices [--remove ID]          list paired phones, or remove one (its connections close)
metabot-app service install|remove|status  keep the server running across logins (systemd user unit
                                           or launchd agent; no root)
```

What the app claims, and the test behind each claim: [docs/CLAIMS.md](docs/CLAIMS.md).
The self-test that runs those claims on Linux and on a phone: [docs/SELFTEST.md](docs/SELFTEST.md).
Pairing a phone: [docs/PAIRING.md](docs/PAIRING.md). Posting a receipt: [docs/RECEIPT-WALL.md](docs/RECEIPT-WALL.md).
The demos that prove MetaBot on itself (watchdog loop, tiers, the PC screen as a tool): [docs/DEMOS.md](docs/DEMOS.md).
The Linux desktop window: [docs/DESKTOP.md](docs/DESKTOP.md).
