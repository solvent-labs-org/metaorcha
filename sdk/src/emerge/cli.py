"""``emerge`` CLI — the launch-scope developer surface.

Three commands only (resist scope creep — test/deploy/login are post-launch):

- ``emerge init [name]``   scaffold a new agent from the bundled template
- ``emerge run [module]``  serve decorated agents locally + register them
- ``emerge publish [module]``  register decorated agents against a remote registry
- ``emerge validate``  validator demo (``--once`` synthetic attestation)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .client import DEFAULT_REGISTRY_URL, RegistryError, register
from .manifest import manifest_yaml
from .sdk import AgentSpec, clear_registry, registered_agents
from .server import serve_agent

logger = logging.getLogger("emerge")

_TEMPLATE_DIR = Path(__file__).parent / "templates" / "your-first-agent"


def _dan_experimental_enabled() -> bool:
    """True when ORCHA_DAN_EXPERIMENTAL=1 (experimental network opt-in)."""
    return os.getenv("ORCHA_DAN_EXPERIMENTAL", "").lower() in ("1", "true", "yes")


def _require_dan_experimental(feature: str) -> bool:
    if _dan_experimental_enabled():
        return True
    print(
        f"emerge: {feature} requires experimental network mode.\n"
        "  Set ORCHA_DAN_EXPERIMENTAL=1 or network.experimental: true in emerge.yaml.",
        file=sys.stderr,
    )
    return False


def _load_module(module_path: str) -> None:
    """Import a Python file by path so its @emerge.agent decorators register."""
    path = Path(module_path).resolve()
    if not path.exists():
        sys.exit(f"emerge: no such file: {module_path}")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        sys.exit(f"emerge: cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)


def _discover(module_path: str) -> list[AgentSpec]:
    clear_registry()
    _load_module(module_path)
    agents = registered_agents()
    if not agents:
        sys.exit(
            f"emerge: no @emerge.agent found in {module_path}. "
            "Did you decorate a handler?"
        )
    return agents


def _default_module() -> str:
    for candidate in ("agent.py", "main.py"):
        if Path(candidate).exists():
            return candidate
    sys.exit(
        "emerge: no agent.py/main.py here. Pass a module path or run `emerge init`."
    )


def cmd_init(args: argparse.Namespace) -> int:
    name = args.name
    slug = name.lower().replace(" ", "-")
    dest = Path(args.dir or slug)
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"emerge: {dest} already exists and is not empty.")
    dest.mkdir(parents=True, exist_ok=True)
    for src in _TEMPLATE_DIR.rglob("*"):
        rel = src.relative_to(_TEMPLATE_DIR)
        target = dest / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        text = src.read_text(encoding="utf-8")
        text = text.replace("{{AGENT_NAME}}", name).replace("{{AGENT_SLUG}}", slug)
        target.write_text(text, encoding="utf-8")
    print(f"✓ Scaffolded '{name}' in {dest}/")
    print("  Next:")
    print(f"    cd {dest}")
    print("    emerge run          # serve locally + register against local registry")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    module = args.module or _default_module()
    agents = _discover(module)
    registry_url = args.registry or os.getenv(
        "ORCHA_REGISTRY_URL", DEFAULT_REGISTRY_URL
    )
    token = os.getenv("ORCHA_PAT")

    # Serve every agent but the last in background threads; block on the last.
    for spec in agents:
        block = spec is agents[-1]
        serve_agent(spec, block=False)
        print(f"✓ Serving {spec.name} on http://localhost:{spec.port}  ({spec.did})")
        if args.register:
            try:
                resp = register(
                    manifest_yaml(spec), registry_url=registry_url, token=token
                )
                print(
                    f"  ✓ Registered with {registry_url}"
                    + (
                        f" (agent_id={resp.get('agent_id')})"
                        if resp.get("agent_id")
                        else ""
                    )
                )
            except RegistryError as exc:
                print(f"  ⚠ Registration skipped: {exc}", file=sys.stderr)
        if block:
            print("\nPress Ctrl+C to stop.")
            try:
                serve_agent(spec, block=True)
            except KeyboardInterrupt:
                print("\nStopped.")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    if getattr(args, "network", None) and not _require_dan_experimental(
        "emerge publish --network"
    ):
        return 1
    module = args.module or _default_module()
    agents = _discover(module)
    registry_url = args.registry or os.getenv("ORCHA_REGISTRY_URL")
    if not registry_url:
        sys.exit("emerge publish: pass --registry <url> or set ORCHA_REGISTRY_URL.")
    token = args.token or os.getenv("ORCHA_PAT")
    host = args.host
    failures = 0
    for spec in agents:
        try:
            resp = register(
                manifest_yaml(spec, host=host), registry_url=registry_url, token=token
            )
            print(
                f"✓ Published {spec.name} → {registry_url}"
                + (
                    f" (agent_id={resp.get('agent_id')})"
                    if resp.get("agent_id")
                    else ""
                )
            )
        except RegistryError as exc:
            failures += 1
            print(f"✗ {spec.name}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def _synthetic_attestation(*, validator_did: str) -> dict:
    """Build a demo attestation for `emerge validate --once`."""
    success = True
    content = "ok"
    score = 0.85 if success and content and not content.startswith("Error:") else 0.2
    return {
        "schema_version": "1.0",
        "call_id": "call-validate-demo",
        "agent_id": "did:orcha:agent:demo",
        "validator_did": validator_did,
        "success": success,
        "latency_ms": 42,
        "judge_score": score,
        "notes": "spike-heuristic (--once demo)",
        "observed_at": datetime.now(UTC).isoformat(),
    }


def cmd_validate(args: argparse.Namespace) -> int:
    validator_did = args.validator_did or os.getenv(
        "VALIDATOR_DID", "did:orcha:validator:local"
    )
    if args.once:
        attestation = _synthetic_attestation(validator_did=validator_did)
        print(json.dumps(attestation, indent=2))
        print(
            f"\n✓ Demo attestation for validator {validator_did} "
            "(subscribe to execution.step_complete for live traffic)"
        )
        return 0
    if not _require_dan_experimental("emerge validate (live mode)"):
        return 1
    if args.kafka:
        print(
            "emerge validate: Kafka consumer is not wired yet.\n"
            "  Use --once for a local demo, or run a validator process against "
            "execution.step_complete when KAFKA_ENABLED=true.",
            file=sys.stderr,
        )
        return 1
    print(
        "emerge validate: pass --once for a demo attestation, or --kafka <brokers> "
        "(consumer TBD).",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orcha-sdk", description="Orcha agent developer CLI")
    p.add_argument("--version", action="version", version=f"orcha-sdk {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="scaffold a new agent from a template")
    pi.add_argument("name", help="agent name, e.g. 'My Agent'")
    pi.add_argument("--dir", help="target directory (default: slug of name)")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser(
        "run", help="serve agents locally + register against local registry"
    )
    pr.add_argument(
        "module", nargs="?", help="path to agent module (default: agent.py/main.py)"
    )
    pr.add_argument(
        "--registry", help=f"registry URL (default: {DEFAULT_REGISTRY_URL})"
    )
    pr.add_argument(
        "--no-register",
        dest="register",
        action="store_false",
        help="serve only; do not register",
    )
    pr.set_defaults(func=cmd_run, register=True)

    pp = sub.add_parser("publish", help="register agents against a remote registry")
    pp.add_argument(
        "module", nargs="?", help="path to agent module (default: agent.py/main.py)"
    )
    pp.add_argument("--registry", help="remote registry URL (or ORCHA_REGISTRY_URL)")
    pp.add_argument("--token", help="PAT token (or ORCHA_PAT)")
    pp.add_argument(
        "--host",
        default="localhost",
        help="host advertised in the manifest endpoint (default: localhost)",
    )
    pp.add_argument(
        "--network",
        help="network bootstrap peer for publish (experimental)",
    )
    pp.set_defaults(func=cmd_publish)

    pv = sub.add_parser(
        "validate",
        help="run a validator observer node (experimental — --once demo)",
    )
    pv.add_argument(
        "--validator-did",
        help="validator DID (default: did:orcha:validator:local or VALIDATOR_DID)",
    )
    pv.add_argument(
        "--bootstrap",
        help="network bootstrap peer (reserved)",
    )
    pv.add_argument(
        "--kafka",
        help="Kafka bootstrap servers for execution.step_complete (consumer TBD)",
    )
    pv.add_argument(
        "--once",
        action="store_true",
        help="emit one synthetic attestation and exit (local demo)",
    )
    pv.set_defaults(func=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("EMERGE_LOG_LEVEL", "INFO"))
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
