"""{{AGENT_NAME}} — your first Orcha agent.

An agent is just a function that takes a natural-language `task` string and
returns a result string. The `@emerge.agent` decorator does the rest:

  - gives the agent a DID (did:orcha:agent:{{AGENT_SLUG}})
  - generates the emerge.yaml manifest the registry validates
  - serves it over an A2A-compatible HTTP endpoint when you run it

Try it:

    orcha-sdk run            # serve + register against the local registry
    orcha-sdk run --no-register   # serve only

Then ask the orchestrator to do something this agent can handle, or call it
directly:

    curl -s localhost:8900/.well-known/agent.json
"""

from __future__ import annotations

import emerge


@emerge.agent(
    name="{{AGENT_NAME}}",
    description="Describe in one sentence what this agent does — the planner reads this to decide when to route to you.",
    version="0.1.0",
    port=8900,
    tags=["example"],
    # Declare the things your agent can do. Skills are surfaced on the agent
    # card and harvested by the registry. Good descriptions + examples make the
    # planner route to you correctly.
    skills=[
        {
            "id": "echo",
            "name": "Echo",
            "description": "Echo the task back. Replace this with your real capability.",
            "examples": ["say hello", "repeat after me: orcha"],
        }
    ],
    # Uncomment to charge per invocation (mock mode by default — no wallet needed):
    # base_fee="0.05",
)
def handle(task: str) -> str:
    """Handle one task. Replace the body with your real logic.

    The handler may be sync or async (`async def handle(...)`).
    """
    return f"{{AGENT_NAME}} received: {task}"


if __name__ == "__main__":
    emerge.run()
