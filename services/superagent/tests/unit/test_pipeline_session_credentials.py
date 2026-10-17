from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from superagent.graph.state import SESSION_CREDENTIALS_CONFIG_KEY
from superagent.middleware.pipeline import ExecutionMiddleware


@pytest.mark.asyncio
async def test_pipeline_uses_config_session_credentials_when_state_missing():
    captured: dict[str, object] = {}

    class FakePreFlightManager:
        def __init__(self, _vault):
            pass

        async def run(self, **kwargs):
            captured["session_credentials"] = kwargs.get("session_credentials")
            return {"manifest": {"transport": {}}, "headers": {}, "resolved_env": None}

    state = {"user_id": "u1", "session_id": "s1"}
    # AD-14 (story 1.6b): the credentials ride on the run config under the
    # double-underscore key LangGraph keeps out of checkpoint metadata; graph
    # state never carries them.
    config = {
        "configurable": {
            SESSION_CREDENTIALS_CONFIG_KEY: {"did:agent:test": {"API_KEY": "secret"}}
        }
    }

    with (
        patch("superagent.middleware.pipeline.PreFlightManager", FakePreFlightManager),
        patch(
            "superagent.middleware.pipeline.InputGuard.validate",
            side_effect=lambda args, _schema: args,
        ),
        patch.object(
            ExecutionMiddleware, "_get_capability_schema", AsyncMock(return_value=None)
        ),
        patch.object(ExecutionMiddleware, "_dispatch", AsyncMock(return_value="ok")),
        patch(
            "superagent.middleware.pipeline.OutputNormalizer.normalize",
            AsyncMock(return_value={"content": "ok"}),
        ),
        patch("superagent.vault.client.VaultClient"),
    ):
        middleware = ExecutionMiddleware(state=state)
        result = await middleware.execute(
            agent_id="did:agent:test",
            capability_id="cap_test",
            protocol="MCP",
            tool_name="did_agent_test__cap_test",
            args={"query": "hello"},
            call_id="call_1",
            config=config,
        )

    assert result["content"] == "ok"
    assert captured["session_credentials"] == {"did:agent:test": {"API_KEY": "secret"}}
