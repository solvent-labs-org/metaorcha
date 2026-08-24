# orcha-sdk

The developer SDK + `emerge` CLI for [Metaorcha](https://github.com/solvent-labs-org/metaorcha) —
an orchestration runtime that plans, routes, and executes one goal across agents
speaking MCP, A2A, and COMPUTER_USE (ACP is accepted as an A2A-routed alias).

Register an agent in three lines:

```python
import emerge

@emerge.agent(name="My Agent", description="What I do")
def handle(task: str) -> str:
    return f"handled: {task}"
```

Then serve it:

```bash
orcha-sdk run          # serve locally + register against the local registry
```

## Install

No clone, no venv setup — just run it:

```bash
uvx orcha-sdk init my-agent
```

Or install it into a project:

```bash
pip install orcha-sdk
```

(Import name is `emerge`; distribution name is `orcha-sdk`.)

## CLI

| Command | What it does |
|---|---|
| `orcha-sdk init "My Agent"` | scaffold a new agent from a template |
| `orcha-sdk run [module]` | serve decorated agents locally + register them against `http://localhost:8000` |
| `orcha-sdk publish [module] --registry <url>` | register against a remote registry |

`orcha-sdk run --no-register` serves without registering. Set `ORCHA_REGISTRY_URL`
and `ORCHA_PAT` to point at and authenticate against a non-local registry.

## How it works

The decorator records your handler and declared skills. `orcha-sdk run` serves an
A2A-compatible HTTP endpoint (`/health`, `/.well-known/agent.json`, JSON-RPC
`message/send` / `tasks/get`) using only the standard library, and uploads a
generated `emerge.yaml` to the registry. The runtime's planner can then discover
and orchestrate your agent alongside agents that speak other protocols.

Apache 2.0 runtime, MIT SDK. See the [main repo](https://github.com/solvent-labs-org/metaorcha).
