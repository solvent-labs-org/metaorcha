"""Build a schema-shaped emerge.yaml for a user-owned MCP. No secrets in the file."""

from __future__ import annotations

import json
import re

_DID_SLUG = re.compile(r"[^a-z0-9._-]+")

# An environment-variable name, nothing else: ``auth_var`` is interpolated into
# the yaml unquoted (``env:`` key, ``${VAR}`` and ``token_vault_ref``), so a
# colon, newline or ``#`` would rewrite the document a user does not otherwise
# control. Same pattern at the route (422) and here (ValueError).
AUTH_VAR_PATTERN = r"^[A-Z_][A-Z0-9_]{0,63}$"
_AUTH_VAR = re.compile(AUTH_VAR_PATTERN)


def agent_did_from_name(name: str) -> str:
    slug = _DID_SLUG.sub("-", name.strip().lower()).strip("-")[:48] or "mcp"
    return f"did:orcha:agent:{slug}"


def build_mcp_emerge_yaml(
    *,
    name: str,
    transport: str,
    endpoint: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    auth_var: str | None = None,
    description: str | None = None,
) -> str:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    did = agent_did_from_name(name)
    desc = (description or f"User MCP: {name}").strip()
    auth = (auth_var or "").strip() or None
    if auth and not _AUTH_VAR.fullmatch(auth):
        raise ValueError(
            "auth_var must be an environment variable name "
            "(uppercase letters, digits, underscore; max 64 chars)"
        )
    transport = transport.strip().lower()

    if transport == "stdio":
        cmd = (command or "").strip()
        if not cmd:
            raise ValueError("command is required for stdio MCP")
        args_yaml = ""
        if args:
            args_yaml = "\n    args:\n" + "\n".join(
                f"      - {json.dumps(a)}" for a in args
            )
        env_yaml = f'\n    env:\n      {auth}: "${{{auth}}}"' if auth else ""
        transport_block = (
            f"  transport:\n    type: stdio\n    command: {json.dumps(cmd)}"
            f"{args_yaml}{env_yaml}"
        )
        health = "http://127.0.0.1:9/health"
    elif transport == "sse":
        ep = (endpoint or "").strip()
        if not ep.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an http(s) URL for SSE MCP")
        transport_block = f"  transport:\n    type: sse\n    endpoint: {json.dumps(ep)}"
        health = ep
    else:
        raise ValueError("transport must be sse or stdio")

    auth_block = ""
    if auth:
        auth_block = (
            "\n  auth_strategies:\n"
            "    - id: strategy_bearer\n"
            "      type: http_bearer\n"
            "      config:\n"
            f"        token_vault_ref: {auth}"
        )

    return (
        "identity:\n"
        f"  id: {json.dumps(did)}\n"
        f"  name: {json.dumps(name)}\n"
        '  version: "1.0.0"\n'
        f"  description: {json.dumps(desc)}\n"
        "  tags:\n    - mcp\n    - user\n\n"
        'protocol:\n  type: mcp\n  version: "1.0"\n'
        f"{transport_block}\n\n"
        f"health_endpoint: {json.dumps(health)}\n\n"
        "security:\n  transport_layer:\n    type: none"
        f"{auth_block}\n\n"
        "payment:\n  enabled: false\n"
    )
