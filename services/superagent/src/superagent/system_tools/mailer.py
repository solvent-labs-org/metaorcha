"""Mailer system tool — email a run's Verified Runs audit as a plain-text receipt.

Sandbox demo tool (`did:orcha:system:mailer`, capability `send_run_receipt`).
Registered only when ``SANDBOX_MAILER=true``. The receipt body is a fixed
template — no user-controlled content beyond the run's own goal/steps.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from .registry import SystemToolRegistry, SystemToolSpec

if TYPE_CHECKING:
    from ..api.models import RunAuditResponse

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_FROM = "Orcha Sandbox <receipts@orcha.ai>"
_SUBJECT = "Your Orcha run receipt"
_REPO_LINE = "Run it yourself: https://github.com/solvent-labs-org/metaorcha"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GOAL_MAX_LEN = 200
_CAP_TTL_SECONDS = 86400  # 1 email per user per day

_redis_client: Any = None


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        from ..config import settings

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _cap_key(user_id: str) -> str:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"mailer:cap:{user_id}:{day}"


async def _cap_reached(user_id: str) -> bool:
    """True when the user already sent a receipt today. Fail-open if Redis is down."""
    try:
        return bool(await _get_redis().get(_cap_key(user_id)))
    except Exception:
        logger.warning("mailer: Redis unavailable for cap check", exc_info=True)
        return False


async def _mark_cap(user_id: str) -> None:
    try:
        await _get_redis().set(_cap_key(user_id), "1", ex=_CAP_TTL_SECONDS)
    except Exception:
        logger.warning("mailer: Redis unavailable for cap write", exc_info=True)


def _render_receipt(audit: RunAuditResponse) -> str:
    """Render the fixed plain-text receipt template from a run audit."""
    lines = [
        "Your Orcha run receipt",
        "",
        f"Goal: {audit.goal[:_GOAL_MAX_LEN]}",
        "",
        "Steps:",
    ]
    for step in audit.steps:
        verdict = "verified" if step.verified else "failed"
        agent_tail = step.agent_id.rsplit(":", 1)[-1]
        line = (
            f"  {step.seq}. {agent_tail} · {step.protocol or '—'} · "
            f"{verdict} · {step.verdict_reason}"
        )
        if step.total_cost_usd:
            line += f" · ${step.total_cost_usd}"
        lines.append(line)
    summary = audit.summary
    lines += [
        "",
        (
            f"Summary: {summary.total_steps} steps — "
            f"{summary.steps_verified} verified, {summary.steps_failed} failed"
        ),
        f"Total cost: ${summary.total_cost_usd}",
    ]
    if summary.duration_ms is not None:
        lines.append(f"Duration: {summary.duration_ms} ms")
    lines += ["", _REPO_LINE]
    return "\n".join(lines)


async def _send_email(api_key: str, to_email: str, body: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": _FROM,
                "to": [to_email],
                "subject": _SUBJECT,
                "text": body,
            },
        )
        resp.raise_for_status()


async def _send_run_receipt(args: dict[str, Any], state: dict[str, Any]) -> str:
    """Email the session's run audit as a fixed-template plain-text receipt."""
    from ..api.audit import build_run_audit
    from ..persistence.transcript_store import load_transcript_rows

    to_email = str(args.get("to_email") or "").strip()
    if not _EMAIL_RE.match(to_email):
        return "Error: invalid email address"

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        return "Error: mailer not configured"

    session_id = str(args.get("session_id") or state.get("session_id") or "").strip()
    if not session_id:
        return "Error: session_id is required"

    user_id = str(state.get("user_id") or "anonymous")
    if await _cap_reached(user_id):
        return "Error: receipt email limit reached (1/day)"

    rows = await load_transcript_rows(session_id)
    body = _render_receipt(build_run_audit(session_id, rows))

    try:
        await _send_email(api_key, to_email, body)
    except Exception:
        logger.exception("mailer: failed to send receipt to %s", to_email)
        return "Error: failed to send receipt"

    await _mark_cap(user_id)
    return f"Receipt sent to {to_email} for session {session_id}"


def register_mailer_tools(registry: SystemToolRegistry) -> None:
    from ..config import settings

    if not settings.sandbox_mailer:
        return
    registry.register(
        SystemToolSpec(
            name="send_run_receipt",
            description=(
                "Email the run's audit (goal, per-step verdicts, cost) as a "
                "plain-text receipt. Pass the visitor's email and the session_id "
                "to receipt. At most one receipt per user per day."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to_email": {
                        "type": "string",
                        "description": "Recipient email address",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session id of the run to receipt",
                    },
                },
                "required": ["to_email", "session_id"],
            },
            handler=_send_run_receipt,
        )
    )
