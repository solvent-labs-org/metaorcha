"""The output a run step commits to — raw, redacted, before display (AD-16).

``output_hash`` in an RFC 0003 step is taken over
``emerge.preimage.output_preimage(raw_result)`` after credential redaction —
never over the ``OutputNormalizer`` display copy, which caps text at 280
characters and replaces binary with an artifact reference. This module
collects the credentials resolved for one call from PreFlight's result and
builds that value for the observer seam.

The display copy, the checklist, criteria and the ToolMessage are untouched:
only the value the observer hashes changes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Same placeholder grammar PreFlight resolves (preflight._PLACEHOLDER_RE).
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
# "Bearer <token>", "token <token>", "Basic <b64>": agents echo the bare token,
# so the part after the scheme word is a credential of its own.
_SCHEME_VALUE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*\s+(\S.*)$", re.DOTALL)


def _platform_env_keys(manifest: Mapping[str, Any]) -> set[str]:
    """Env keys filled from the platform's own secrets (``platform_env``)."""
    keys: set[str] = set()
    security = manifest.get("security")
    strategies = (
        security.get("auth_strategies") if isinstance(security, Mapping) else None
    )
    for strategy in strategies if isinstance(strategies, list) else []:
        if not isinstance(strategy, Mapping):
            continue
        if str(strategy.get("type") or "").lower() != "platform_env":
            continue
        config = strategy.get("config")
        env_key = config.get("env_key") if isinstance(config, Mapping) else None
        if isinstance(env_key, str) and env_key:
            keys.add(env_key)
    return keys


def _template_secrets(env_key: str, template: str, value: str) -> list[tuple[str, str]]:
    """Credentials inside one resolved ``transport.env`` entry.

    A template with exactly one ``${VAR}`` yields VAR's exact secret: the
    literal text around the placeholder is stripped by an anchored match. With
    several placeholders the split is ambiguous, so the whole resolved value is
    redacted under the env key instead.
    """
    names = _PLACEHOLDER_RE.findall(template)
    if not names:
        return []
    if len(names) == 1:
        prefix, _, suffix = _PLACEHOLDER_RE.split(template, maxsplit=1)
        match = re.fullmatch(
            re.escape(prefix) + "(.*)" + re.escape(suffix), value, re.DOTALL
        )
        if match:
            # An empty secret has no bytes to redact.
            return [(names[0], match.group(1))] if match.group(1) else []
    return [(env_key, value)]


def call_credentials(
    manifest: Any, resolved_env: Any, auth_headers: Any
) -> list[tuple[str, str]]:
    """Every credential PreFlight resolved for one call, as ``(VAR, secret)``.

    - each auth header value (and the token after a scheme word), named by
      the header;
    - each ``transport.env`` value resolved from a ``${VAR}`` placeholder,
      named by the placeholder;
    - each ``platform_env`` value, named by its env key.

    Literal ``transport.env`` values (``LOG_LEVEL=info``) are configuration,
    not credentials, and are left alone. Never raises.
    """
    pairs: list[tuple[str, str]] = []
    try:
        if isinstance(auth_headers, Mapping):
            for name, value in auth_headers.items():
                if not isinstance(name, str) or not isinstance(value, str) or not value:
                    continue
                pairs.append((name, value))
                scheme = _SCHEME_VALUE_RE.match(value)
                if scheme:
                    pairs.append((name, scheme.group(1)))

        if isinstance(resolved_env, Mapping) and resolved_env:
            manifest = manifest if isinstance(manifest, Mapping) else {}
            transport = manifest.get("transport")
            templates = transport.get("env") if isinstance(transport, Mapping) else None
            templates = templates if isinstance(templates, Mapping) else {}
            platform_keys = _platform_env_keys(manifest)
            for env_key, value in resolved_env.items():
                if (
                    not isinstance(env_key, str)
                    or not isinstance(value, str)
                    or not value
                ):
                    continue
                if env_key in platform_keys:
                    pairs.append((env_key, value))
                template = templates.get(env_key)
                if isinstance(template, str):
                    pairs.extend(_template_secrets(env_key, template, value))
    except Exception:
        logger.exception("call_credentials: could not collect credentials")
    return pairs


def step_output_preimage(raw_output: Any, credentials: list[tuple[str, str]]) -> Any:
    """The redacted pre-image of *raw_output*, or None when it cannot be built.

    None tells the run-attestation observer to fall back to the display
    content. That happens only where the SDK is not installed (the SuperAgent
    image ships without it, and without the validator that consumes this).
    Never raises.
    """
    try:
        from emerge.preimage import (  # noqa: PLC0415 — optional workspace package
            output_preimage,
            redact_credentials,
        )
    except ImportError:
        return None
    try:
        return redact_credentials(output_preimage(raw_output), credentials)
    except Exception:
        logger.exception("step_output_preimage: could not build the pre-image")
        return None
