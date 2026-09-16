"""User MCP manifest builder — secrets never land in the yaml."""

from __future__ import annotations

from gateway.plugins.mcp_manifest import agent_did_from_name, build_mcp_emerge_yaml


def test_did_slug():
    assert agent_did_from_name("My Weather") == "did:orcha:agent:my-weather"


def test_sse_yaml_has_no_secret():
    text = build_mcp_emerge_yaml(
        name="Docs MCP",
        transport="sse",
        endpoint="https://example.com/mcp",
        auth_var="MCP_TOKEN",
    )
    assert "type: mcp" in text
    assert "type: sse" in text
    assert "token_vault_ref: MCP_TOKEN" in text
    assert "sk-" not in text
    assert "did:orcha:agent:docs-mcp" in text


def test_stdio_requires_command():
    try:
        build_mcp_emerge_yaml(name="x", transport="stdio")
    except ValueError as exc:
        assert "command" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_sse_requires_url():
    try:
        build_mcp_emerge_yaml(name="x", transport="sse", endpoint="not-a-url")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
