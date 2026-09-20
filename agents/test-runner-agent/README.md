# Test Runner (A2A)

Runs **one allow-listed test command** in **one fixed directory** and returns the
result as JSON: `{"exit_code", "stdout_tail", "duration_ms", "command", "repo"}`.
It exists so a run can declare `exit_zero` and have a step that actually carries
an exit code. It is charged (`base_fee: "0.01"`, mock mode) so the settle gate
sees it.

The task text chooses nothing. Command and directory come from the environment:

| Variable | Default | Meaning |
|---|---|---|
| `TEST_RUNNER_REPO` | — (required) | directory the command runs in |
| `TEST_RUNNER_CMD` | `pytest -q` | one of `pytest -q`, `pytest`, `npm test`, `make test`, `go test ./...`, `cargo test` |
| `TEST_RUNNER_TIMEOUT` | `300` | seconds before the command is killed |

On any problem (unset repo, command outside the allowlist, timeout) the JSON
carries `error` and **no** `exit_code`, so a declared `exit_zero` fails closed.

## Run

```bash
pip install orcha-sdk==0.1.3 pytest
TEST_RUNNER_REPO=$PWD/fixtures/passing python agent.py     # serves :8903, registers if a registry is up
curl -s localhost:8903/.well-known/agent.json
```

In the sandbox it is the `test-runner` service; switch `TEST_RUNNER_REPO` in
`.env.sandbox` to `/app/fixtures/failing` to produce a refused settle.

## Test

```bash
PYTHONPATH=sdk/src python -m pytest agents/test-runner-agent/tests -q
```

## Manifest

`emerge.yaml` is generated from the decorator and checked by a test:

```bash
PYTHONPATH=sdk/src:agents/test-runner-agent python -c \
  "import agent; from emerge.manifest import manifest_yaml; from emerge.sdk import registered_agents; print(manifest_yaml(registered_agents()[0]), end='')" \
  > agents/test-runner-agent/emerge.yaml
```
