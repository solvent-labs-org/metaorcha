"""The Claude Code plugin package (story 4.2): a launcher and nothing else.

``plugins/claude-code/orcha-record`` must subscribe to exactly the events
the adapter handles, run the SDK's hook and no logic of its own, never exit
2 (a receipt never blocks a tool call or a Stop), and ship the spec's golden
pair unchanged for the sixty-second path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from emerge.hooks import claude_code_settings

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "claude-code" / "orcha-record"
GOLDEN = REPO / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"
LAUNCHER = PLUGIN / "hooks" / "record.sh"

pytestmark = pytest.mark.skipif(
    not PLUGIN.is_dir(), reason="plugin directory not present (sdist install)"
)


def test_manifest_names_the_plugin_and_its_hooks_file():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "orcha-record"
    assert manifest["hooks"] == "./hooks/hooks.json"
    assert (PLUGIN / "hooks" / "hooks.json").is_file()
    # the manifest's copy carries the honesty line's object, not a claim
    assert "verif" in manifest["description"].lower()
    for word in ("first", "only", "unlike"):
        assert word not in manifest["description"].lower(), word


def test_hooks_file_subscribes_to_exactly_the_adapters_events():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    expected = claude_code_settings("x")["hooks"]
    assert set(hooks) == set(expected) == {"PostToolUse", "PostToolUseFailure", "Stop"}
    for event, matchers in hooks.items():
        (matcher,) = matchers
        assert "matcher" not in matcher, f"{event}: every tool is recorded"
        (hook,) = matcher["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/record.sh"'


def test_launcher_is_executable_and_runs_only_the_sdk_hook():
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR
    text = LAUNCHER.read_text()
    assert "orcha record hook claude-code" in text
    assert "exit 2" not in text
    # the launcher reads nothing from the payload: no jq, no python, no parsing
    for forbidden in ("jq ", "python", "json", "session_id", "tool_name"):
        assert forbidden not in text, forbidden


def _run_launcher(tmp_path: Path, orcha_exit: int | None) -> int:
    """Run the launcher with uvx hidden and an ``orcha`` stub (or none)."""
    bin_dir = tmp_path / f"bin-{orcha_exit}"
    bin_dir.mkdir()
    if orcha_exit is not None:
        stub = bin_dir / "orcha"
        stub.write_text(f"#!/bin/sh\ncat >/dev/null\nexit {orcha_exit}\n")
        stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["bash", str(LAUNCHER)],
        input=b"{}",
        env=env,
        capture_output=True,
        timeout=30,
    )
    return proc.returncode


@pytest.mark.skipif(os.name != "posix", reason="bash launcher")
def test_launcher_maps_every_failure_to_one_and_never_blocks(tmp_path):
    assert _run_launcher(tmp_path, 0) == 0
    assert _run_launcher(tmp_path, 1) == 1
    # a stale package whose argparse rejects `record` exits 2: never passed on
    assert _run_launcher(tmp_path, 2) == 1
    assert _run_launcher(tmp_path, 127) == 1
    # neither uvx nor orcha on PATH: told, not blocked
    assert _run_launcher(tmp_path, None) == 1


def test_examples_are_the_golden_pair_unchanged():
    golden = json.loads(GOLDEN.read_text())
    for name in ("valid", "tampered"):
        example = json.loads((PLUGIN / "examples" / f"golden-{name}.json").read_text())
        assert example == golden[name], name


def test_readme_carries_the_honesty_lines_and_no_claims():
    text = " ".join((PLUGIN / "README.md").read_text().lower().split())
    for required in (
        "self-signed",
        "what ran and nothing more",
        "does not prove the work was correct",
        "settlement and refusal stay on the server path",
    ):
        assert required in text, required
    # the sixty-second path comes before any mention of the stack
    for later in ("docker", "services/", "postgres", "kafka"):
        assert later not in text, later
    for claim in ("first to ", "the only ", "nobody", "unlike ", "competitor"):
        assert claim not in text, claim


def test_the_install_command_the_readme_gives_resolves_to_this_plugin():
    # `/plugin install orcha-record@<marketplace>` works only if the repo root
    # carries a marketplace of that name listing this directory
    readme = (PLUGIN / "README.md").read_text()
    market_path = REPO / ".claude-plugin" / "marketplace.json"
    assert "/plugin marketplace add solvent-labs-org/metaorcha" in readme
    assert market_path.is_file(), "README installs from a marketplace the repo lacks"
    market = json.loads(market_path.read_text())
    assert f"/plugin install orcha-record@{market['name']}" in readme
    (entry,) = [p for p in market["plugins"] if p["name"] == "orcha-record"]
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert entry["name"] == manifest["name"]
    # the no-plugin path prints the same three events the plugin subscribes to
    assert "orcha record hook claude-code --print-settings" in readme
