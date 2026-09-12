"""Orcha agent CLI — the launch-scope developer surface.

Installed as ``orcha-sdk`` (and ``emerge``, kept as an alias). Five commands
only (resist scope creep — test/deploy/login are post-launch):

- ``orcha-sdk init [name]``   scaffold a new agent from the bundled template
- ``orcha-sdk run [module]``  serve decorated agents locally + register them
- ``orcha-sdk publish [module]``  register decorated agents against a remote registry
- ``orcha-sdk validate``  validator demo (``--once`` synthetic attestation)
- ``orcha-sdk verify <envelope.json>``  offline run attestation verifier (RFC 0003)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .client import DEFAULT_REGISTRY_URL, RegistryError, register
from .manifest import manifest_yaml
from .run_attestation import AttestationVerdict, verify_run_attestation
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
    print("    orcha-sdk run       # serve locally; registers if a registry is up")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    module = args.module or _default_module()
    agents = _discover(module)
    registry_url = args.registry or os.getenv(
        "ORCHA_REGISTRY_URL", DEFAULT_REGISTRY_URL
    )
    token = os.getenv("ORCHA_PAT")

    # Every agent serves in a background thread first: the registry harvests
    # capabilities from the live endpoint, so it has to be reachable before
    # register() is called. The main thread parks afterwards — serving a port
    # that is already bound raises OSError(EADDRINUSE).
    for spec in agents:
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
                print(
                    "    The agent is serving locally regardless. To register it, "
                    "start the runtime (./scripts/run-all.sh) or pass --no-register "
                    "to silence this.",
                    file=sys.stderr,
                )

    print("\nPress Ctrl+C to stop.")
    try:
        threading.Event().wait()
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


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify an RFC 0003 run attestation envelope, fully offline."""
    if args.resolve_did:
        print(
            "emerge verify: --resolve-did is not yet supported — the SDK has no "
            "DID resolution infrastructure.\n"
            "  Re-run without --resolve-did: default verification is fully "
            "offline against the envelope's embedded signer.public_key_b64.",
            file=sys.stderr,
        )
        return 2

    try:
        if args.envelope == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.envelope).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"emerge verify: cannot read {args.envelope}: {exc}", file=sys.stderr)
        return 2
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"emerge verify: malformed JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(envelope, dict):
        print(
            "emerge verify: envelope must be a JSON object "
            f"(got {type(envelope).__name__})",
            file=sys.stderr,
        )
        return 2

    verdict = verify_run_attestation(envelope)
    if args.json:
        print(
            json.dumps(
                {
                    "valid": verdict.valid,
                    "verdict": verdict.verdict,
                    "run_id": verdict.run_id,
                    "signer_did": verdict.signer_did,
                    "step_count": verdict.step_count,
                    "checks": verdict.checks,
                }
            )
        )
    else:
        _print_verdict(verdict)
    return 0 if verdict.valid else 1


def _print_verdict(verdict: AttestationVerdict) -> None:
    def _mark(passed: bool) -> str:
        return "ok" if passed else "FAILED"

    print("Run attestation verification (orcha.run-attestation/v1, offline)")
    print(f"  run_id:    {verdict.run_id or 'n/a'}")
    print(f"  signer:    {verdict.signer_did or 'n/a'}")
    print(
        f"  steps:     {verdict.step_count if verdict.step_count is not None else 'n/a'}"
    )
    print(f"  schema:      {_mark(verdict.checks['schema'])}")
    print(f"  steps_root:  {_mark(verdict.checks['steps_root'])}")
    print(f"  merkle_root: {_mark(verdict.checks['steps_merkle_root'])}")
    print(f"  signature:   {_mark(verdict.checks['signature'])}")
    print(f"Verdict: {verdict.verdict.upper()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Orcha agent developer CLI")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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

    pver = sub.add_parser(
        "verify",
        help="verify a signed run attestation envelope offline (RFC 0003)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Verification is fully offline: schema check, step hash-chain\n"
            "recomputation, steps_root comparison, and Ed25519 signature\n"
            "verification against the envelope's embedded signer key.\n"
            "No network calls are made.\n"
            "\n"
            "exit codes:\n"
            "  0  envelope is valid\n"
            "  1  envelope is invalid (schema, chain, or signature check failed)\n"
            "  2  usage/input error (unreadable file, malformed JSON, unsupported option)"
        ),
    )
    pver.add_argument(
        "envelope",
        help="path to the envelope JSON file, or '-' to read from stdin",
    )
    pver.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON result instead of human-readable output",
    )
    pver.add_argument(
        "--resolve-did",
        action="store_true",
        help="resolve the signer DID document to cross-check the embedded key "
        "(not yet supported — fails with a clear message)",
    )
    pver.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("EMERGE_LOG_LEVEL", "INFO"))
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
