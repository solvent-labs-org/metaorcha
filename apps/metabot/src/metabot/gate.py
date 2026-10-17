"""The one decision every call goes through: allow, ask a human, or deny.

* read passes;
* write passes when the job allows writes, or allows this exact tool;
  otherwise a human is asked;
* destructive is never allowed by policy: a human must approve that exact
  call;
* with no human present, anything that would ask is denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from metabot.scope_classes import ScopeClass


@dataclass(frozen=True)
class Policy:
    allow_write: bool = False
    allows: frozenset[str] = field(default_factory=frozenset)
    interactive: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "allow_write": self.allow_write,
            "allows": sorted(self.allows),
            "interactive": self.interactive,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Policy:
        return cls(
            allow_write=bool(data.get("allow_write", False)),
            allows=frozenset(str(a) for a in data.get("allows", [])),
            interactive=bool(data.get("interactive", True)),
        )


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "ask" | "deny"
    reason: str
    allow: str = ""  # a write the policy let through: which allow, as the receipt names it


def decide(tool: str, cls: ScopeClass, policy: Policy) -> Decision:
    if cls is ScopeClass.READ:
        return Decision("allow", "read")
    if cls is ScopeClass.WRITE:
        if policy.allow_write:
            return Decision("allow", "writes allowed for this job", "allow_write")
        if tool in policy.allows:
            return Decision("allow", f"write allowed: {tool}", f"allows {tool}")
        if not policy.interactive:
            return Decision("deny", "no human present")
        return Decision("ask", "write needs approval")
    if not policy.interactive:
        return Decision("deny", "no human present")
    return Decision("ask", "destructive: a human must approve this exact call")
