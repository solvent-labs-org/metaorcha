"""Record input_tokens beside output_tokens; billing formula stays output-only."""

from __future__ import annotations

from superagent.nodes.orchestrator import tokens_from_usage


def test_reads_input_and_output_from_usage() -> None:
    inp, out = tokens_from_usage({"input_tokens": 120, "output_tokens": 40}, "ignored")
    assert inp == 120
    assert out == 40


def test_missing_input_is_zero() -> None:
    inp, out = tokens_from_usage({"output_tokens": 12}, "hello")
    assert inp == 0
    assert out == 12


def test_missing_usage_estimates_output_only() -> None:
    inp, out = tokens_from_usage(None, "abcd")
    assert inp == 0
    assert out == 1


def test_output_fallback_does_not_invent_input() -> None:
    inp, out = tokens_from_usage({"input_tokens": 99}, "abcdefgh")
    assert inp == 99
    assert out == 2
