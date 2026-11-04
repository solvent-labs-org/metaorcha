"""User connections — a tool server plus the user's own token (story 1.3).

A connection is an agent the Gateway connect route registered; its manifest
carries the ``connection`` tag. Two rules apply to connections only, so an
agent registered any other way behaves exactly as before:

- ``CONNECTIONS_ENABLED`` off (the default) means a connection is never
  called (spine AD-18).
- A connection whose token strategy finds no credential fails with
  ``credential_missing`` before any request reaches the platform — no health
  probe, no auth interrupt. The user adds the token from the connect form.
- A removed connection (story 1.7) fails with ``connection_revoked``, and one
  whose Registry record cannot be read now fails with
  ``connection_unavailable`` — both before any request (AD-6).
"""

from __future__ import annotations

from typing import Any

CONNECTION_TAG = "connection"
CONNECTIONS_DISABLED = (
    "connections_disabled: calling a connection is turned off on this "
    "deployment (CONNECTIONS_ENABLED)"
)
CREDENTIAL_MISSING = "credential_missing"
CONNECTION_REVOKED = (
    "connection_revoked: this connection was removed; connect the tool again to use it"
)
CONNECTION_UNAVAILABLE = "connection_unavailable"


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


def connection_unavailable(reason: str) -> str:
    return (
        f"{CONNECTION_UNAVAILABLE}: {reason}; the call was not sent "
        "(a connection is only called on a current Registry record)"
    )
