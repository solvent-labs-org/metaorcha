"""Test Runner — an A2A agent that runs one allow-listed test command and
reports the exit code.

Why it exists: the acceptance walk needs a step whose result is machine-
checkable. A run that declares ``exit_zero`` needs a step that carries an exit
code; this agent is that step. The task text never chooses the command — the
command and the repository come from the environment, so a prompt cannot make
this agent run anything else.

Output (the A2A text artifact) is one JSON object::

    {"exit_code": 0, "stdout_tail": "...", "duration_ms": 412,
     "command": "pytest -q", "repo": "/app/fixtures/passing"}

On a configuration or execution problem the object carries ``error`` and **no**
``exit_code``, so a declared ``exit_zero`` criterion reads "no exit code" and
fails closed rather than passing on a run that never happened.

Environment:
    TEST_RUNNER_REPO     directory to run in (required)
    TEST_RUNNER_CMD      one of ALLOWED_COMMANDS (default: ``pytest -q``)
    TEST_RUNNER_TIMEOUT  seconds before the command is killed (default: 300)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import emerge

# Command string → argv. pytest runs through this interpreter so the agent does
# not depend on a `pytest` launcher being on PATH (it is not, inside a bare venv
# call); the others are exec'd as written. No shell is ever involved.
ALLOWED_COMMANDS: dict[str, list[str]] = {
    "pytest -q": [sys.executable, "-m", "pytest", "-q"],
    "pytest": [sys.executable, "-m", "pytest"],
    "npm test": ["npm", "test"],
    "make test": ["make", "test"],
    "go test ./...": ["go", "test", "./..."],
    "cargo test": ["cargo", "test"],
}
DEFAULT_COMMAND = "pytest -q"
DEFAULT_TIMEOUT_S = 300
TAIL_CHARS = 2000


def _config() -> tuple[str, str, int]:
    repo = os.environ.get("TEST_RUNNER_REPO", "").strip()
    cmd = os.environ.get("TEST_RUNNER_CMD", DEFAULT_COMMAND).strip() or DEFAULT_COMMAND
    try:
        timeout = int(os.environ.get("TEST_RUNNER_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return repo, cmd, timeout


def run_tests(task: str) -> dict[str, Any]:
    """Run the configured command in the configured repo. Never raises."""
    del task  # the task text is logged by the runtime; it selects nothing here
    repo, cmd, timeout = _config()
    if cmd not in ALLOWED_COMMANDS:
        return {
            "error": f"command not allow-listed: {cmd!r}",
            "allowed": sorted(ALLOWED_COMMANDS),
        }
    if not repo or not Path(repo).is_dir():
        return {
            "error": "TEST_RUNNER_REPO is unset or not a directory",
            "repo": repo,
        }
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv from a fixed allowlist, no shell
            ALLOWED_COMMANDS[cmd],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
    except FileNotFoundError as exc:
        return {"error": f"command not found: {exc.filename}", "command": cmd}
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s", "command": cmd, "repo": repo}
    duration_ms = int((time.monotonic() - started) * 1000)
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "exit_code": int(proc.returncode),
        "stdout_tail": output[-TAIL_CHARS:],
        "duration_ms": duration_ms,
        "command": cmd,
        "repo": repo,
    }


@emerge.agent(
    name="Test Runner",
    description=(
        "Runs one allow-listed test command (default `pytest -q`) in a fixed "
        "repository and returns the exit code, the output tail and the duration "
        "as JSON. The task text does not choose the command."
    ),
    version="0.1.0",
    port=8903,
    tags=["tests", "ci", "exit-code", "a2a"],
    skills=[
        {
            "id": "run_tests",
            "name": "Run tests",
            "description": (
                "Run the configured test command and report "
                "{exit_code, stdout_tail, duration_ms}."
            ),
            "tags": ["tests", "exit-code"],
            "examples": [
                "run the test suite",
                "run the tests and report the exit code",
            ],
        }
    ],
    # Charged so the settle gate sees this step (mock mode: no wallet needed).
    base_fee="0.01",
)
def handle(task: str) -> str:
    return json.dumps(run_tests(task), sort_keys=True)


if __name__ == "__main__":
    emerge.run()
