"""Routine save rules — what a routine may hold (story 2.1, FR-23, AD-18, AD-19).

The Gateway persists a routine; the SuperAgent decides whether its contents are
allowed, because the scope-class rules (AD-18) and the criteria vocabulary live
here and nowhere else. ``check_routine`` either returns the resolved class of
every allow or raises ``RoutineRejected`` naming the field and the reason —
before anything is persisted.

Rules:

- ``connections``: at least one; each must be a current, active connection
  read fresh from the Registry (a cached copy is never trusted, as in
  PreFlight).
- ``scope_allow``: ``"<connection DID>#<capability>"`` (AD-17's step form). The
  DID must be one of ``connections``; the capability must be one the
  connection exposes; its class is resolved by ``resolve_scope_class`` —
  ``destructive`` is rejected, and a capability the class rules do not
  recognise resolves to ``destructive``, so it is rejected on the same path.
- ``criteria``: ``dict[str, bool]``, at most 8 keys, every key one the
  SuperAgent evaluates (``SUPPORTED_CRITERIA``) — the chat path's rule.
- ``criteria_operands``: keyed by declared criteria only; each a flat object of
  short printable-ASCII scalars, because operands are echoed into the
  criterion's verdict ``detail`` (AD-19) and the SDK verifier requires
  printable ASCII there.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..middleware.connections import is_connection
from ..middleware.criteria import SUPPORTED_CRITERIA
from ..middleware.scope_classes import (
    ScopeClass,
    is_recognised,
    is_valid_capability_id,
    resolve_scope_class,
)

MAX_CRITERIA = 8
MAX_OPERANDS = 8
MAX_OPERAND_CHARS = 200

ManifestReader = Callable[[str], Awaitable[dict[str, Any]]]

# the Gateway mints connection DIDs in this shape (plugins/routes.py); the id
# becomes one path segment of a Registry URL, so nothing else is accepted
_CONNECTION_DID = re.compile(r"did:orcha:agent:[A-Za-z0-9._-]{1,128}")


class RoutineRejected(ValueError):
    """A routine that must not be saved. ``field`` names the offending field."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def _printable_ascii(value: str) -> bool:
    return all(" " <= ch <= "~" for ch in value)


def check_criteria(criteria: Any, operands: Any) -> None:
    if not isinstance(criteria, dict):
        raise RoutineRejected("criteria", "must be an object of name → boolean")
    if len(criteria) > MAX_CRITERIA:
        raise RoutineRejected("criteria", f"at most {MAX_CRITERIA} criteria")
    for key, value in criteria.items():
        if key not in SUPPORTED_CRITERIA:
            raise RoutineRejected("criteria", f"unsupported criterion: {key}")
        if not isinstance(value, bool):
            raise RoutineRejected("criteria", f"{key} must be true or false")
    if not isinstance(operands, dict):
        raise RoutineRejected("criteria_operands", "must be an object")
    for key, spec in operands.items():
        if key not in criteria:
            raise RoutineRejected(
                "criteria_operands", f"{key} is not a declared criterion"
            )
        if not isinstance(spec, dict) or len(spec) > MAX_OPERANDS:
            raise RoutineRejected(
                "criteria_operands",
                f"{key} must be an object of at most {MAX_OPERANDS} operands",
            )
        for name, value in spec.items():
            text = str(value)
            if (
                not isinstance(name, str)
                or not _printable_ascii(name)
                or isinstance(value, dict | list)
                or value is None
                or len(text) > MAX_OPERAND_CHARS
                or not _printable_ascii(text)
            ):
                raise RoutineRejected(
                    "criteria_operands",
                    f"{key}.{name} must be a short printable-ASCII value",
                )


def _capabilities(manifest: dict[str, Any]) -> set[str]:
    return {
        str(c.get("capability_id") or c.get("id") or "")
        for c in manifest.get("capabilities") or []
        if isinstance(c, dict)
    }


async def check_routine(
    *,
    connections: Any,
    scope_allow: Any,
    criteria: Any,
    criteria_operands: Any,
    read_manifest: ManifestReader,
) -> dict[str, str]:
    """Validate a routine; return ``{"<DID>#<capability>": class}`` for its allows.

    ``read_manifest`` must read the Registry now and raise on failure (the
    caller passes ``MANIFEST_CACHE.get_manifest(..., fresh=True)``).
    """
    check_criteria(criteria, criteria_operands)

    if not isinstance(connections, list) or not connections:
        raise RoutineRejected("connections", "a routine needs at least one connection")
    manifests: dict[str, dict[str, Any]] = {}
    for did in connections:
        if not isinstance(did, str) or not _CONNECTION_DID.fullmatch(did):
            raise RoutineRejected("connections", f"{did!r} is not a connection")
        try:
            manifest = await read_manifest(did)
        except Exception:
            raise RoutineRejected(
                "connections", f"{did} could not be read from the Registry"
            ) from None
        if manifest.get("is_active") is False:
            raise RoutineRejected("connections", f"{did} was removed")
        if manifest.get("is_active") is not True or not is_connection(manifest):
            raise RoutineRejected("connections", f"{did} is not an active connection")
        manifests[did] = manifest

    if not isinstance(scope_allow, list):
        raise RoutineRejected(
            "scope_allow", "must be a list of <connection DID>#<capability>"
        )
    classes: dict[str, str] = {}
    for entry in scope_allow:
        did, sep, capability = str(entry).partition("#")
        if not sep or did not in manifests:
            raise RoutineRejected(
                "scope_allow",
                f"{entry} does not name one of this routine's connections",
            )
        if not is_valid_capability_id(capability):
            raise RoutineRejected(
                "scope_allow", f"{entry} is not a valid capability id"
            )
        manifest = manifests[did]
        if capability not in _capabilities(manifest):
            raise RoutineRejected(
                "scope_allow", f"{capability} is not a capability of {did}"
            )
        declared = manifest.get("scope_classes")
        scope_class = resolve_scope_class(
            capability,
            manifest_classes=declared if isinstance(declared, dict) else None,
        )
        if scope_class is ScopeClass.DESTRUCTIVE:
            why = (
                "is destructive"
                if is_recognised(capability)
                else "is not in the scope-class table, so it resolves to destructive,"
            )
            raise RoutineRejected(
                "scope_allow",
                f"{capability} {why} and can never be allowed on a routine "
                "(a destructive call always waits for a person)",
            )
        classes[entry] = scope_class.label
    return classes


__all__ = [
    "MAX_CRITERIA",
    "RoutineRejected",
    "check_criteria",
    "check_routine",
]
