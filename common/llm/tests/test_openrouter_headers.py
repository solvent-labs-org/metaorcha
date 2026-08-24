"""Guard against brand regressions in outbound OpenRouter headers (OSS hard rule #2)."""

from common.llm.src import LLMConfig, OpenRouterProvider


def test_openrouter_headers_use_orcha_brand():
    provider = OpenRouterProvider(LLMConfig(api_key="test-key"))
    headers = provider._client.headers
    assert headers["X-Title"] == "Orcha"
    assert headers["HTTP-Referer"] == "https://github.com/solvent-labs-org/metaorcha"
    # No legacy brand beyond the GitHub org name (solvent-labs-org) in the Referer.
    scrubbed = str(dict(headers)).lower().replace("solvent-labs-org", "")
    assert "metaorcha" not in scrubbed
