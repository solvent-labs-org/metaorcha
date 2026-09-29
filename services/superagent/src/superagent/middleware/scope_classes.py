"""Scope classes — every capability resolves to read | write | destructive.

The platform owns the class *rules* (AD-18):

* a committed table classing every platform system tool;
* name-pattern rules for connector capabilities, keyed on the verb tokens
  in the capability id (``delete_*``, ``merge_*``, ``force_*``, ``close_*``
  → destructive);
* a connection's manifest may declare classes, and a user may override —
  either may only *tighten*. The effective class is the strictest of the
  three sources.

A connector capability absent from every source resolves to ``destructive``
(fail closed, AD-6). The lookup never raises: an empty, null or malformed id
is ``destructive``.

System tools are classed by consequence. None is ``destructive`` and a test
asserts it — a destructive system tool would pause every chat turn. The
``write``-class system tools act on the platform's own session state (or, for
the sandbox mailer, on an address the operator configured); the scope gate
that consumes this table treats them as covered by the platform's standing
allow. That gate is story 1.5; this module only classifies.

The capability charset ``[A-Za-z0-9_.:-]+`` is the one AD-17 fixes for the
``<connection DID>#<capability>`` step tool string; the Registry rejects any
capability outside it at registration.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any

CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ScopeClass(IntEnum):
    """Ordered so that ``max()`` is the strictest."""

    READ = 0
    WRITE = 1
    DESTRUCTIVE = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: Any) -> ScopeClass | None:
        """``"read"``/``"write"``/``"destructive"`` → member; anything else → None."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                return None
        return None


# ── Platform system tools ────────────────────────────────────────────────────
# Every name registered by ``system_tools.register_all_system_tools`` must be
# here; ``test_scope_classes.py`` enumerates the live registry against it.

SYSTEM_TOOL_CLASSES: dict[str, ScopeClass] = {
    # artifacts
    "read_artifact": ScopeClass.READ,
    "save_artifact": ScopeClass.WRITE,
    "stage_artifact": ScopeClass.WRITE,
    # attestation
    "sign_case_attestation": ScopeClass.WRITE,
    # checklist (session state)
    "create_checklist": ScopeClass.WRITE,
    "add_checklist_step": ScopeClass.WRITE,
    "update_checklist_step": ScopeClass.WRITE,
    "abandon_checklist": ScopeClass.WRITE,
    # enforcement
    "propose_enforcement": ScopeClass.WRITE,
    # mailer (sandbox, gated on SANDBOX_MAILER=true)
    "send_run_receipt": ScopeClass.WRITE,
    # memory
    "query_memory": ScopeClass.READ,
    "store_to_memory": ScopeClass.WRITE,
    # utils
    "get_datetime": ScopeClass.READ,
    "list_tools": ScopeClass.READ,
    # workflow
    "save_as_workflow_template": ScopeClass.WRITE,
}


# ── Connector name-pattern rules ─────────────────────────────────────────────
# A capability id is split on ``_ - . :`` and camelCase boundaries into verb
# tokens. The strictest token wins (``create_or_delete`` is destructive);
# ``force`` anywhere is destructive (``force_push``); no recognised token is
# destructive.

_DESTRUCTIVE_TOKENS = frozenset(
    {
        "delete",
        "remove",
        "merge",
        "force",
        "close",
        "archive",
        "purge",
        "drop",
        "destroy",
        "revoke",
        "truncate",
        "reset",
        "rm",
        "wipe",
        "unpublish",
        "kick",
        "ban",
    }
)

_WRITE_TOKENS = frozenset(
    {
        "create",
        "add",
        "post",
        "update",
        "patch",
        "put",
        "push",
        "write",
        "send",
        "set",
        "move",
        "edit",
        "upload",
        "publish",
        "assign",
        "comment",
        "reply",
        "insert",
        "fork",
        "transfer",
        "rename",
        "restore",
        "submit",
        "open",
        "reopen",
        "star",
        "unstar",
        "invite",
        "label",
        "react",
        "reaction",
        "tag",
        "pin",
        "unpin",
        "save",
        "store",
        "append",
        "duplicate",
        "clone",
        "run",
        "execute",
        "trigger",
        "dispatch",
        "schedule",
    }
)

_READ_TOKENS = frozenset(
    {
        "list",
        "get",
        "search",
        "read",
        "fetch",
        "query",
        "describe",
        "show",
        "find",
        "view",
        "browse",
        "count",
        "check",
        "lookup",
        "resolve",
        "download",
        "export",
        "retrieve",
        "head",
        "info",
        "status",
        "history",
        "diff",
        "compare",
        "preview",
        "validate",
        "verify",
    }
)

_SPLIT = re.compile(r"[_\-.:]+|(?<=[a-z0-9])(?=[A-Z])")


def _tokens(capability: str) -> list[str]:
    return [t.lower() for t in _SPLIT.split(capability) if t]


def platform_rule(capability: Any) -> ScopeClass:
    """The platform's own class for a capability id — never raises.

    System tools resolve from the table; connector capabilities from the
    verb tokens; anything unrecognised, empty or malformed is ``destructive``.
    """
    if not isinstance(capability, str) or not capability.strip():
        return ScopeClass.DESTRUCTIVE
    table = SYSTEM_TOOL_CLASSES.get(capability)
    if table is not None:
        return table
    if not CAPABILITY_PATTERN.match(capability):
        return ScopeClass.DESTRUCTIVE
    tokens = _tokens(capability)
    if any(t in _DESTRUCTIVE_TOKENS for t in tokens):
        return ScopeClass.DESTRUCTIVE
    if any(t in _WRITE_TOKENS for t in tokens):
        return ScopeClass.WRITE
    if any(t in _READ_TOKENS for t in tokens):
        return ScopeClass.READ
    return ScopeClass.DESTRUCTIVE


def resolve_scope_class(
    capability: Any,
    *,
    manifest_classes: dict[str, Any] | None = None,
    override: Any = None,
) -> ScopeClass:
    """Effective class = strictest of platform rule, manifest and override.

    ``manifest_classes`` maps capability id → declared class (from the
    connection's manifest); ``override`` is a user-supplied class for this
    capability. A declaration that would *loosen* the platform rule is
    ignored; an unparseable declaration is ignored. Never raises.
    """
    effective = platform_rule(capability)
    if isinstance(manifest_classes, dict) and isinstance(capability, str):
        declared = ScopeClass.parse(manifest_classes.get(capability))
        if declared is not None:
            effective = max(effective, declared)
    declared_override = ScopeClass.parse(override)
    if declared_override is not None:
        effective = max(effective, declared_override)
    return effective


def is_valid_capability_id(capability: Any) -> bool:
    """True iff the id fits AD-17's charset ``[A-Za-z0-9_.:-]+`` (so never ``#``)."""
    return isinstance(capability, str) and bool(CAPABILITY_PATTERN.match(capability))


__all__ = [
    "CAPABILITY_PATTERN",
    "SYSTEM_TOOL_CLASSES",
    "ScopeClass",
    "is_valid_capability_id",
    "platform_rule",
    "resolve_scope_class",
]
