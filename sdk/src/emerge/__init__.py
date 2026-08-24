"""Orcha agent SDK — register an agent in three lines.

    import emerge

    @emerge.agent(name="My Agent", description="What I do")
    def handle(task: str) -> str:
        return f"handled: {task}"

    if __name__ == "__main__":
        emerge.run()

Or use the CLI: ``emerge run`` / ``emerge publish``.
"""

from __future__ import annotations

import sys

from .manifest import build_manifest, manifest_yaml
from .sdk import AgentSpec, Skill, agent, registered_agents

__version__ = "0.1.1"

__all__ = [
    "agent",
    "Skill",
    "AgentSpec",
    "registered_agents",
    "build_manifest",
    "manifest_yaml",
    "run",
    "__version__",
]


def run() -> int:
    """Serve every decorated agent in this process (convenience for ``__main__``).

    Agents are already registered via import, so this just serves them — it does
    not register against a registry. Use the ``emerge run`` CLI for registration.
    """
    from .server import serve_agent

    agents = registered_agents()
    if not agents:
        print(
            "emerge.run(): no @emerge.agent registered in this module.", file=sys.stderr
        )
        return 1
    for spec in agents[:-1]:
        serve_agent(spec, block=False)
        print(f"✓ Serving {spec.name} on http://localhost:{spec.port}  ({spec.did})")
    last = agents[-1]
    print(f"✓ Serving {last.name} on http://localhost:{last.port}  ({last.did})")
    print("Press Ctrl+C to stop.")
    try:
        serve_agent(last, block=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0
