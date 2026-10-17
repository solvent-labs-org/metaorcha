"""The ``@emerge.agent`` decorator and the in-process agent registry.

Register an agent in three lines::

    import emerge

    @emerge.agent(name="My Agent", description="What I do")
    def handle(task: str) -> str:
        return f"handled: {task}"

Then ``emerge run`` (CLI) — or ``emerge.run()`` in ``__main__`` — serves it over
an A2A-compatible HTTP endpoint and registers it against a local registry.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[[str], Any]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


@dataclass
class Skill:
    """A declared capability, surfaced on the agent card and in the manifest."""

    id: str
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id.replace("-", " ").replace("_", " ").title()


@dataclass
class AgentSpec:
    """Everything the runtime needs to serve, describe, and register an agent."""

    name: str
    description: str
    handler: Handler
    did: str
    version: str = "0.1.0"
    port: int = 8900
    tags: list[str] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    base_fee: str | None = None

    async def invoke(self, task: str) -> str:
        """Call the handler, awaiting it if it is a coroutine. Returns text."""
        result = self.handler(task)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, str) else str(result)


# Process-wide registry of decorated agents, in declaration order.
_AGENTS: list[AgentSpec] = []


def agent(
    _fn: Handler | None = None,
    *,
    name: str,
    description: str,
    did: str | None = None,
    version: str = "0.1.0",
    port: int = 8900,
    tags: list[str] | None = None,
    skills: list[Skill | dict[str, Any]] | None = None,
    base_fee: str | None = None,
) -> Callable[[Handler], Handler] | Handler:
    """Register ``fn`` as a Metaorcha agent. Returns ``fn`` unchanged.

    ``did`` defaults to ``did:orcha:agent:<slug-of-name>``. If no ``skills`` are
    given, a single skill is derived from the agent name so the agent card is
    never empty (the registry harvests skills from the card at registration).
    """

    def decorate(fn: Handler) -> Handler:
        slug = _slugify(name)
        resolved_did = did or f"did:orcha:agent:{slug}"
        resolved_skills: list[Skill] = []
        for s in skills or []:
            resolved_skills.append(s if isinstance(s, Skill) else Skill(**s))
        if not resolved_skills:
            resolved_skills = [
                Skill(id=slug, name=name, description=description, tags=tags or [])
            ]
        _AGENTS.append(
            AgentSpec(
                name=name,
                description=description,
                handler=fn,
                did=resolved_did,
                version=version,
                port=port,
                tags=tags or [],
                skills=resolved_skills,
                base_fee=base_fee,
            )
        )
        return fn

    # Support both @agent(...) and bare @agent usage.
    if _fn is not None:
        return decorate(_fn)
    return decorate


def registered_agents() -> list[AgentSpec]:
    """Return all agents registered via the decorator, in declaration order."""
    return list(_AGENTS)


def clear_registry() -> None:
    """Test helper — drop all registered agents."""
    _AGENTS.clear()
