"""The one place a call's approval is decided and its verdict recorded.

Both the gate hook and the switchboard send their questions here, as the
control socket's ``approve`` message: ``{"call_id", "tool", "cls",
"preview"}``. The answer is ``{"allow": bool, "reason": str}``. A human's
approval, every decline, and every write the policy lets through become
verdicts, so the receipt says who or what let each write run. A read does
not.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from metabot.approvals import ApprovalRequest, Approver
from metabot.gate import Policy, decide
from metabot.receipts import allow_verdict, approval_verdict
from metabot.scope_classes import ScopeClass


class Decider:
    def __init__(self, policy: Policy, approver: Approver, signer: str) -> None:
        if approver.kind != "human":
            policy = replace(policy, interactive=False)
        self.policy = policy
        self.approver = approver
        self.signer = signer
        self.verdicts: list[dict[str, str]] = []

    async def __call__(self, msg: dict[str, Any]) -> dict[str, Any]:
        call_id = str(msg.get("call_id") or "unknown")
        tool = str(msg.get("tool") or "unknown")
        parsed = ScopeClass.parse(msg.get("cls"))  # READ is 0, so never "parsed or ..."
        cls = ScopeClass.DESTRUCTIVE if parsed is None else parsed
        decision = decide(tool, cls, self.policy)
        if decision.action == "allow":
            if decision.allow:
                self.verdicts.append(allow_verdict(call_id, decision.allow))
            return {"allow": True, "reason": decision.reason}
        if decision.action == "deny":
            self.verdicts.append(approval_verdict(call_id, approved=False, reason=decision.reason))
            return {"allow": False, "reason": decision.reason}
        request = ApprovalRequest(call_id, tool, cls, str(msg.get("preview") or ""))
        try:
            allowed = await self.approver.ask(request)
        except asyncio.CancelledError:  # the run ended while a person was being asked
            self.verdicts.append(approval_verdict(call_id, approved=False, reason="cancelled"))
            raise
        except Exception as exc:  # an approver that breaks declines
            self.verdicts.append(approval_verdict(call_id, approved=False, reason="approver error"))
            return {"allow": False, "reason": f"approver error: {type(exc).__name__}"}
        if allowed:
            self.verdicts.append(approval_verdict(call_id, approved=True, approver=self.signer))
            return {"allow": True, "reason": f"{cls.label} approved by a human"}
        self.verdicts.append(approval_verdict(call_id, approved=False))
        return {"allow": False, "reason": f"{cls.label} declined"}
