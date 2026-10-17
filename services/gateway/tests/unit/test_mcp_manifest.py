"""User MCP manifest builder — secrets never land in the yaml."""

from __future__ import annotations

from gateway.plugins.mcp_manifest import (
    CONNECTION_TAG,
    agent_did_from_name,
    build_mcp_emerge_yaml,
    mint_connection_did,
)


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


def test_auth_var_must_be_an_env_var_name():
    # Unquoted in the yaml: a colon or newline would rewrite the document.
    for bad in ("MCP TOKEN", "mcp_token", "X: y", "A\nB", "1ABC", "A" * 65, "#"):
        try:
            build_mcp_emerge_yaml(
                name="x", transport="sse", endpoint="https://e.com", auth_var=bad
            )
        except ValueError as exc:
            assert "auth_var" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
    text = build_mcp_emerge_yaml(
        name="x", transport="sse", endpoint="https://e.com", auth_var="_A1_TOKEN"
    )
    assert "token_vault_ref: _A1_TOKEN" in text


def test_minted_did_is_unique_per_registration():
    first, second = mint_connection_did("My Weather"), mint_connection_did("My Weather")
    assert first != second
    assert first.startswith("did:orcha:agent:my-weather-")
    assert mint_connection_did("My Weather", "abcd1234") == (
        "did:orcha:agent:my-weather-abcd1234"
    )


def test_yaml_pins_the_given_did_and_carries_the_connection_tag():
    text = build_mcp_emerge_yaml(
        name="Docs MCP",
        transport="sse",
        endpoint="https://example.com/mcp",
        did="did:orcha:agent:docs-mcp-abcd1234",
    )
    assert 'id: "did:orcha:agent:docs-mcp-abcd1234"' in text
    assert f"    - {CONNECTION_TAG}\n" in text
