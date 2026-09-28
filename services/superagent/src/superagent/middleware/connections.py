"""User connections — a tool server plus the user's own token (story 1.3).

A connection is an agent the Gateway connect route registered; its manifest
carries the ``connection`` tag. Two rules apply to connections only, so an
agent registered any other way behaves exactly as before:

- ``CONNECTIONS_ENABLED`` off (the default) means a connection is never
  called (spine AD-18).
- A connection whose token strategy finds no credential fails with
  ``credential_missing`` before any request reaches the platform — no health
  probe, no auth interrupt. The user adds the token from the connect form.
"""

from __future__ import annotations

from typing import Any

CONNECTION_TAG = "connection"
CONNECTIONS_DISABLED = (
    "connections_disabled: calling a connection is turned off on this "
    "deployment (CONNECTIONS_ENABLED)"
)
CREDENTIAL_MISSING = "credential_missing"


def is_connection(manifest: Any) -> bool:
    tags = manifest.get("tags") if isinstance(manifest, dict) else None
    return isinstance(tags, list) and CONNECTION_TAG in tags


def connections_enabled() -> bool:
    from ..config import settings  # noqa: PLC0415 — read at call time

    return bool(getattr(settings, "connections_enabled", False))


def credential_missing(var: str) -> str:
    return (
        f"{CREDENTIAL_MISSING}: this connection needs {var}; "
        "add it from the connect form"
    )
