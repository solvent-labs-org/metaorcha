"""Operator gate — who may register a ``stdio`` transport (AD-13).

A stdio connection launches a subprocess on the platform host with variables
resolved from the vault. That is an operator's decision, never a member's or a
guest's. The Gateway connect route and the Registry's ``register_agent`` /
``update_agent`` both call :func:`is_operator`; the two doors must agree, so
the rule lives here and nowhere else.

An operator is a user id listed in ``OPERATOR_USER_IDS`` (comma-separated), or
anyone at all when ``DISABLE_AUTH=true`` — the same switch that already turns
off authentication everywhere else, so a local stack keeps working unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

STDIO_OPERATOR_ONLY = (
    "stdio_operator_only: a stdio transport launches a subprocess on the "
    "platform host and may only be registered by an operator "
    "(OPERATOR_USER_IDS)"
)


def operator_user_ids(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The configured operator ids — trimmed, empty entries dropped."""
    raw = (env if env is not None else os.environ).get("OPERATOR_USER_IDS", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_operator(user_id: str | None, env: Mapping[str, str] | None = None) -> bool:
    """True iff ``user_id`` may register stdio transports.

    Never raises; an empty or missing id is never an operator unless auth is
    disabled outright.
    """
    source = env if env is not None else os.environ
    if source.get("DISABLE_AUTH", "false").strip().lower() == "true":
        return True
    if not isinstance(user_id, str) or not user_id.strip():
        return False
    return user_id.strip() in operator_user_ids(source)


__all__ = ["STDIO_OPERATOR_ONLY", "is_operator", "operator_user_ids"]
