"""Scope gate — a write pauses for a human, a destructive call always does.

One ``scope_gate()`` is called at every dispatch site (AD-18): the pipeline
slot between PreFlight and Dispatch, and the system-tool call site in the
execute node. ``test_scope_gate.py`` enumerates the sites, so a new one
cannot omit it.

Decisions, in order:

- ``CONNECTIONS_ENABLED`` off → the gate is never invoked; the caller checks
  ``connections_enabled()`` first, so the flags-off path is byte-identical
  to today's (story 3.4). Nothing in this module reads the flag.
- A platform system tool is ``read`` or ``write`` (never ``destructive``,
  story 1.2 asserts it) and acts on the platform's own state: covered by
  the platform's standing allow, it passes.
- A ``read`` capability passes.
- A ``write`` capability passes when the connection's manifest declares it
  in ``scope_allow``; otherwise it pauses.
- A ``destructive`` capability pauses every time — no allow list, override
  or prior approval of a different call skips it.

On a routine firing (story 2.2, AD-18/AD-19) nobody is present to approve,
so the routine's own bounds replace the pause for everything but a
destructive call:

- a connection that is not one of the routine's is refused;
- a ``write`` passes only when the routine allows ``<DID>#<capability>``,
  and is otherwise refused with ``scope_not_allowed`` — never paused;
- a ``destructive`` call still pauses the firing, and is never
  auto-approved; the routine's owner decides it from the firing's session.

A refusal is ``ScopeNotAllowed``, a hard failure: nothing is dispatched.

A pause is ``ScopeApprovalRequired``, the resumable ``AuthInterruptRequired``
pattern (not the decline-only ``PaymentInterrupt``): the node catches it,
calls ``interrupt()`` with the ``HITL_APPROVAL`` event and, on resume, re-runs
the call with ``scope_approval`` set. A gate given an approval for the same
``call_id`` passes, so a retry of an approved call is not re-gated. The
approval and the decline both reach the receipt as
``scope_approval:<call_id>`` verdicts (AD-21) — see ``scope_verdict``.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from internal_commons.interrupts.events import InterruptEvent
from internal_commons.interrupts.payloads import HitlApprovalMetadata
from internal_commons.interrupts.types import InterruptType

from .scope_classes import SYSTEM_TOOL_CLASSES, ScopeClass, resolve_scope_class

SCOPE_DECLINED = "scope_declined"
SCOPE_NOT_ALLOWED = "scope_not_allowed"
SCOPE_APPROVAL_CHECK = "scope_approval"
USER_DID_PREFIX = "did:orcha:user:"
_TARGET_MAX_CHARS = 200
_TARGET_KEYS = (
    "owner",
    "repo",
    "repository",
    "path",
    "url",
    "title",
    "name",
    "id",
    "channel",
    "to",
    "subject",
    "issue_number",
    "pull_number",
    "branch",
)


class ScopeApprovalRequired(Exception):
    """A call that must wait for a human. Soft and resumable, never a failure."""

    def __init__(self, event: InterruptEvent, scope_class: ScopeClass) -> None:
        super().__init__(event.message)
        self.event = event
        self.scope_class = scope_class


class ScopeNotAllowed(Exception):
    """A call a routine firing may not make. Hard: nothing is dispatched."""


def _routine_allows(routine: dict[str, Any], agent_id: str, capability: str) -> bool:
    allow = routine.get("scope_allow")
    return isinstance(allow, list) and f"{agent_id}#{capability}" in allow


def _routine_connections(routine: dict[str, Any]) -> list[Any]:
    connections = routine.get("connections")
    return connections if isinstance(connections, list) else []


def approver_did(user_id: Any) -> str:
    """The DID a receipt names for the human who decided.

    ``did:orcha:user:<user_id>`` — the id the Gateway stamped on the resume
    payload from the verified JWT, never a free-text client field.
    """
    return f"{USER_DID_PREFIX}{str(user_id or '').strip()}"


def approval_for(call_id: str, resume_value: Any, fallback_user_id: Any) -> dict:
    """Build the approval a resumed call carries: ``{call_id, approver}``."""
    resume = resume_value if isinstance(resume_value, dict) else {}
    user_id = str(resume.get("authoriser_user_id") or "").strip() or str(
        fallback_user_id or ""
    )
    return {"call_id": call_id, "approver": approver_did(user_id)}


def is_approved(resume_value: Any) -> bool:
    status = (
        str(resume_value.get("status") or "").lower()
        if isinstance(resume_value, dict)
        else ""
    )
    return status in ("approved", "approve", "complete")


def scope_verdict(*, approved: bool, approver: str = "") -> dict[str, str]:
    """The ``scope_approval`` entry the pipeline puts in ``StepResult.metadata``.

    The observer turns it into ``{check: "scope_approval:<call_id>", ...}``.
    A decline is ``warn`` and never ``fail`` (AD-21): the gate refused one
    call; the run's other steps are what they are.
    """
    if approved:
        return {"result": "pass", "detail": approver}
    return {"result": "warn", "detail": "declined"}


def target_summary(args: Any) -> str:
    """What the call acts on, for the approval card — short, from the args."""
    if not isinstance(args, dict) or not args:
        return ""
    named = {
        k: args[k] for k in _TARGET_KEYS if k in args and args[k] not in ("", None)
    }
    chosen = named or args
    try:
        text = json.dumps(chosen, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(chosen)
    if len(text) > _TARGET_MAX_CHARS:
        text = text[: _TARGET_MAX_CHARS - 1] + "…"
    return text


def _manifest_scope_classes(manifest: Any) -> dict[str, Any] | None:
    declared = manifest.get("scope_classes") if isinstance(manifest, dict) else None
    return declared if isinstance(declared, dict) else None


def _manifest_allows(manifest: Any, capability: str) -> bool:
    allow = manifest.get("scope_allow") if isinstance(manifest, dict) else None
    return isinstance(allow, list) and capability in allow


def _build_event(
    *,
    scope_class: ScopeClass,
    agent_id: str,
    capability_id: str,
    args: Any,
    call_id: str,
    session_id: str,
    connection_name: str,
) -> InterruptEvent:
    target = target_summary(args)
    verb = "delete or change" if scope_class is ScopeClass.DESTRUCTIVE else "write to"
    description = f"{connection_name} wants to {verb} your platform: {capability_id}"
    if target:
        description += f" on {target}"
    metadata = HitlApprovalMetadata(
        action_description=description,
        risk_level="high" if scope_class is ScopeClass.DESTRUCTIVE else "medium",
        agent_display_name=connection_name,
        capability_name=capability_id,
        scope_class=scope_class.label,
        connection_id=agent_id,
        connection_name=connection_name,
        target=target,
        call_id=call_id,
        capability_id=capability_id,
    )
    return InterruptEvent(
        interrupt_type=InterruptType.HITL_APPROVAL,
        interrupt_id=(
            f"HITL_APPROVAL__{agent_id}__{capability_id}__{secrets.token_hex(4)}"
        ),
        agent_id=agent_id,
        session_id=session_id,
        message=description,
        metadata=metadata.model_dump(),
    )


def scope_gate(
    *,
    agent_id: str,
    capability_id: str,
    args: Any,
    call_id: str,
    session_id: str,
    manifest: Any = None,
    override: Any = None,
    scope_approval: Any = None,
    connection_name: str = "",
    routine: Any = None,
) -> ScopeClass:
    """Pass the call or raise ``ScopeApprovalRequired``. Returns the class.

    ``scope_approval`` is ``{"call_id", "approver"}`` from a resumed call; it
    passes only the ``call_id`` it names. Never returns for a destructive
    call without one.

    ``routine`` is the firing's ``{connections, scope_allow, ...}`` when the
    call is made by a routine firing; then a call outside the routine's
    bounds raises ``ScopeNotAllowed`` instead of pausing (see module doc).
    """
    if agent_id == "_system":
        # Platform standing allow: acts on the platform's own session state.
        # Keyed on the site, not the name: a connection whose capability is
        # called ``save_artifact`` is still a connection.
        return SYSTEM_TOOL_CLASSES.get(capability_id, ScopeClass.WRITE)
    firing = routine if isinstance(routine, dict) else None
    if firing is not None and agent_id not in _routine_connections(firing):
        raise ScopeNotAllowed(
            f"{SCOPE_NOT_ALLOWED}: {agent_id} is not one of this routine's "
            "connections; the call was not sent"
        )
    scope_class = resolve_scope_class(
        capability_id,
        manifest_classes=_manifest_scope_classes(manifest),
        override=override,
    )
    if scope_class is ScopeClass.READ:
        return scope_class
    if (
        isinstance(scope_approval, dict)
        and scope_approval.get("call_id") == call_id
        and scope_approval.get("approver")
    ):
        return scope_class
    if firing is not None and scope_class is ScopeClass.WRITE:
        # Nobody is present to approve: the routine's allows are the only
        # allow list, and a write outside them is refused, not paused.
        if _routine_allows(firing, agent_id, capability_id):
            return scope_class
        raise ScopeNotAllowed(
            f"{SCOPE_NOT_ALLOWED}: {capability_id!r} is a write this routine does "
            "not allow, and nobody is present to approve it; the call was not sent"
        )
    if scope_class is ScopeClass.WRITE and _manifest_allows(manifest, capability_id):
        return scope_class
    name = connection_name or (
        str(manifest.get("name") or "").strip() if isinstance(manifest, dict) else ""
    )
    raise ScopeApprovalRequired(
        _build_event(
            scope_class=scope_class,
            agent_id=agent_id,
            capability_id=capability_id,
            args=args,
            call_id=call_id,
            session_id=session_id,
            connection_name=name or agent_id.rsplit(":", 1)[-1],
        ),
        scope_class,
    )


__all__ = [
    "SCOPE_APPROVAL_CHECK",
    "SCOPE_DECLINED",
    "SCOPE_NOT_ALLOWED",
    "USER_DID_PREFIX",
    "ScopeApprovalRequired",
    "ScopeNotAllowed",
    "approval_for",
    "approver_did",
    "is_approved",
    "scope_gate",
    "scope_verdict",
    "target_summary",
]
