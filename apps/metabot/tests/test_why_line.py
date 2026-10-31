"""The everyday line: what a ladder did and why, in one line of plain words."""

from __future__ import annotations

from fake_chat import FakeChat, answer, refused
from metabot.ladder import ANSWERED, CLIMBED, SKIPPED, STOPPED, LadderResult, RungRow, why_line
from test_web_everyday import _until, configure
from test_web_runs import _post, ui


def _row(n: int, label: str, outcome: str, why: str = "") -> RungRow:
    engine = label.split("@")[0].split("/")[0]
    return RungRow(n, engine, None, "local", outcome, why, label=label)


def test_an_answer_on_the_first_rung_says_so_and_what_it_cost():
    result = LadderResult("l1", [_row(1, "openai@home/small", ANSWERED)], answered=True)
    assert why_line(result) == "Done on openai@home/small, with no climb. Spent $0.00."


def test_each_climb_says_why_then_where_it_was_done():
    rows = [
        _row(1, "openai@home/small", CLIMBED, "its check failed (ask:1 exit 1)"),
        _row(2, "claude/haiku", ANSWERED),
    ]
    result = LadderResult("l1", rows, answered=True, spent_musd=40_000)
    assert why_line(result) == (
        "openai@home/small failed: its check failed (ask:1 exit 1). "
        "Done on claude/haiku. Spent $0.04."
    )


def test_a_skipped_rung_and_a_stop_are_said():
    rows = [
        _row(1, "openai@hosted/big", SKIPPED, "the task reads as private"),
        _row(2, "openai@home/small", STOPPED, "a person declined a call"),
    ]
    result = LadderResult("l1", rows, stopped="a person declined a call")
    assert why_line(result) == (
        "openai@hosted/big skipped: the task reads as private. "
        "Stopped: a person declined a call. Spent $0.00."
    )


def test_a_rung_with_no_price_is_counted_not_hidden():
    result = LadderResult("l1", [_row(1, "codex", ANSWERED)], answered=True, unpriced=1)
    assert why_line(result) == (
        "Done on codex, with no climb. Spent $0.00, and 1 rung had no cost MetaBot could count."
    )


def test_the_home_page_shows_the_line_once_the_ladder_ends(tmp_path, monkeypatch):
    server = FakeChat([answer("all done")])
    try:
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            page = _post(client, guard, "/", prompt="tidy the notes", folder=str(tmp_path))
            run_id = page.headers["location"].removeprefix("/live/")
            text = _until(client, run_id, lambda t: "Spent $" in t)
        assert "Done on openai@home/small, with no climb. Spent $0.00." in text
    finally:
        server.close()


def test_an_escaped_reason_is_cut_to_its_length_so_the_stop_and_spend_stay_in_view():
    rows = [_row(1, "openai@hosted/big", CLIMBED, "\U000e0001" * 500)]
    result = LadderResult("l1", rows, stopped="no rung left to try", spent_musd=0)
    said = why_line(result)
    assert len(said) < 400 and "\U000e0001" not in said
    assert said.endswith(". Stopped: no rung left to try. Spent $0.00.")


def _long(rungs: int = 5) -> LadderResult:
    """Three rungs skipped, then rungs that each failed with a long reason."""
    hosted, home = "openai@a-hosted-endpoint-{}/a-big-model", "openai@the-home-endpoint-{}/small"
    rows = [_row(n, hosted.format(n), SKIPPED, "the task reads as private") for n in (1, 2, 3)]
    rows += [
        _row(n, home.format(n), CLIMBED, "answered 500: " + "x" * 400) for n in range(4, 4 + rungs)
    ]
    return LadderResult("l1", rows, stopped="no rung left to try", unpriced=rungs)


def test_a_line_cut_to_fit_keeps_where_it_stopped_and_what_it_spent():
    whole = why_line(_long())
    said = why_line(_long(), limit=2000)
    assert len(whole) > 2000 and len(said) <= 2000
    tail = "Stopped: no rung left to try. Spent $0.00, and 5 rungs had no cost MetaBot could count."
    assert whole.endswith(tail) and said.endswith("… " + tail)
    assert said.startswith(whole[:1500])
    assert why_line(_long(1), limit=2000) == why_line(_long(1))  # one that fits is whole


def test_the_page_shows_where_a_long_ladder_stopped_and_what_it_spent(tmp_path, monkeypatch):
    server = FakeChat(repeat=refused(500, "the model failed to load: " + "x" * 400))
    try:
        hosted = {
            f"hosted-{n}": {"base_url": f"https://hosted-{n}.invalid/v1", "model": "big"}
            for n in (1, 2, 3)
        }
        local = {f"home-{n}": {"base_url": server.url, "model": "small"} for n in range(4, 9)}
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={**hosted, **local},
            ladder=[{"engine": "openai", "endpoint": name} for name in (*hosted, *local)],
            ladder_attempts=5,
        )
        with ui(tmp_path) as (client, guard):
            page = _post(
                client, guard, "/", prompt="tidy the confidential notes", folder=str(tmp_path)
            )
            run_id = page.headers["location"].removeprefix("/live/")
            text = _until(client, run_id, lambda t: 'class="why"' in t)
        assert "openai@hosted-1" in text and "openai@home-8/small failed" in text
        assert "… Stopped: no rung left to try. Spent $0.00.</p>" in text  # the reasons are cut
    finally:
        server.close()
