# {{AGENT_NAME}}

Your first Orcha agent, scaffolded with `orcha-sdk init`.

## Run it

```bash
pip install orcha-sdk        # if you haven't already
orcha-sdk run                   # serve + register against http://localhost:8000
```

Or serve without registering:

```bash
orcha-sdk run --no-register
```

## What's here

- `agent.py` — the agent. A decorated `handle(task)` function is the whole thing.
- `requirements.txt` — just `orcha-sdk`.

## Next steps

1. Edit `handle()` in `agent.py` with your real logic.
2. Update the `description` and `skills` — the planner uses them to route to you.
3. `orcha-sdk publish --registry <url>` to register against a remote registry.

DID: `did:orcha:agent:{{AGENT_SLUG}}` · Manifest: generated from the decorator.
See the [bridges guide](https://metaorcha.ai/docs)
to connect a whole other protocol.
