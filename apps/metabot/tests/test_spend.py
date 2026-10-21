"""What a run spent: tokens as each engine reports them, and what they cost."""

from __future__ import annotations

import pytest

from metabot.prices import PRICES_AS_OF, Rate, price_for, prices_digest, rate_for, read_prices
from metabot.spend import Spend, dollars, spend_of


@pytest.mark.parametrize(
    ("data", "spend"),
    [
        (  # claude's result: the run's total, and what claude says it cost
            {
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 4,
                },
                "total_cost_usd": 0.0016942900000000002,
            },
            Spend(input=1, output=4, cache_read=3, cache_write=2, reported_musd=1695),
        ),
        (  # codex counts cached input inside its input
            {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
            Spend(input=40, output=20, cache_read=60),
        ),
        (
            {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 30, "cacheWriteTokens": 1},
            Spend(input=10, output=2, cache_read=30, cache_write=1),
        ),
        (
            {
                "prompt_tokens": 70,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 50},
            },
            Spend(input=20, output=3, cache_read=50),
        ),
        ({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, Spend(7, 3)),
        ({}, Spend()),
        ({"input_tokens": "lots", "output_tokens": -5}, Spend()),
        ({"usage": "none"}, Spend()),
    ],
)
def test_each_engines_report_reads_as_one_spend(data, spend):
    assert spend_of(data) == spend


@pytest.mark.parametrize("cost", [True, -1, float("nan"), float("inf"), "0.5", None])
def test_a_reported_cost_that_is_not_a_cost_is_none(cost):
    assert spend_of({"usage": {}, "total_cost_usd": cost}).reported_musd is None


def test_spends_add_and_a_cost_reported_by_one_is_kept():
    total = Spend(1, 2, 3, 4) + Spend(10, 20, reported_musd=7)
    assert total == Spend(11, 22, 3, 4, reported_musd=7)
    assert (Spend(1) + Spend(2)).reported_musd is None
    assert total.tokens == 11 + 22 + 3 + 4


@pytest.mark.parametrize(
    ("musd", "text"),
    [
        (0, "$0.00"),
        (500_000, "$0.50"),
        (25_000_000, "$25.00"),
        (1_694, "$0.001694"),
        (3, "$0.000003"),
    ],
)
def test_dollars_never_shows_a_cost_as_zero_that_is_not(musd, text):
    assert dollars(musd) == text


def test_a_rate_rounds_up_so_a_cost_is_never_under_said():
    rate = Rate(input=4_000_000, output=20_000_000, cache_write=5_000_000, cache_read=200_000)
    assert rate.cost(Spend(input=1)) == 4  # 4 micro-dollars a token
    assert rate.cost(Spend(cache_read=1)) == 1  # 0.2 rounds up
    assert rate.cost(Spend(input=1_000_000, output=1_000_000)) == 24_000_000


def test_a_rate_with_a_long_prompt_tier_uses_it_past_the_threshold():
    long = Rate(input=500_000, output=2_500_000, cache_write=0, cache_read=0)
    rate = Rate(input=100_000, output=500_000, cache_write=0, cache_read=0, above=(100_000, long))
    assert rate.cost(Spend(input=100_000)) == 10_000
    assert rate.cost(Spend(input=100_001)) == 50_001  # 50000.5 rounds up


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-opus-5-5[1m]",
        "claude-opus-5-5-20261001",
        "us.anthropic.claude-opus-5-5",
        "anthropic/claude-opus-5-5",
    ],
)
def test_a_claude_model_id_finds_its_rate(model):
    assert rate_for(model, {}) == rate_for("claude-opus-5-5", {})
    assert rate_for(model, {}) != rate_for("claude-opus-5", {})  # the longer name wins


def test_a_model_the_table_does_not_hold_has_no_rate():
    assert rate_for("gpt-oss-20b", {}) is None
    assert rate_for("claude-opus-5-55", {}) is None  # a prefix is not a name
    assert rate_for("claude-opus-5-5-2026", {}) is None  # nor is any number after it
    assert rate_for(None, {}) is None


def test_a_dated_snapshot_takes_its_models_rate():
    assert rate_for("claude-haiku-4-5-20251001", {}) == rate_for("claude-haiku-4-5", {})


def test_the_engine_decides_the_source():
    assert price_for("openai", "qwen2.5:3b", "http://127.0.0.1:11434/v1", {}).source == "local"
    assert price_for("openai", "qwen2.5:3b", "http://localhost:11434/v1", {}).source == "local"
    assert price_for("openai", "x", "http://[::1]:8080/v1", {}).source == "local"
    assert price_for("openai", "x", "https://api.example.com/v1", {}).source == "unknown"
    assert price_for("openai", "claude-haiku-5-5", "https://api.example.com/v1", {}).source == (
        "table"
    )
    claude = price_for("claude", "claude-sonnet-5-5", None, {})
    assert claude.source == "engine" and claude.rate is not None  # the rate is the running estimate
    assert price_for("codex", "gpt-5", None, {}).source == "plan"
    assert price_for("cursor", None, None, {}).source == "plan"


def test_the_persons_prices_win_over_the_table_and_the_engine():
    mine = read_prices({"gpt-oss-20b": {"in": 0.05, "out": 0.2}})
    hosted = price_for("openai", "gpt-oss-20b", "https://api.example.com/v1", mine)
    assert hosted.source == "config"
    assert hosted.rate == Rate(input=50_000, output=200_000, cache_write=50_000, cache_read=50_000)
    assert price_for("codex", "gpt-oss-20b", None, mine).source == "config"
    local = price_for("openai", "gpt-oss-20b", "http://127.0.0.1:1/v1", mine)
    assert local.source == "config"  # their word for what a local model costs them


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"m": 1},
        {"m": {"in": 1}},
        {"m": {"in": -1, "out": 1}},
        {"m": {"in": True, "out": 1}},
        {"m": {"in": 1, "out": 1, "extra": 1}},
        {"": {"in": 1, "out": 1}},
    ],
)
def test_prices_that_do_not_read_are_refused(bad):
    with pytest.raises(ValueError):
        read_prices(bad)


def test_the_digest_names_the_prices_and_changes_with_them():
    assert prices_digest({}) == prices_digest({})
    assert len(prices_digest({})) == 16
    assert prices_digest(read_prices({"m": {"in": 1, "out": 1}})) != prices_digest({})
    assert PRICES_AS_OF == "2026-10-21"
