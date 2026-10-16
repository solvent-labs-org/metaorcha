"""Five-field cron: when a routine comes due, in its own time zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from metabot.cron import Cron, CronError, zone

LONDON = ZoneInfo("Europe/London")
UTC = UTC


def _at(*args: int, tz=LONDON) -> datetime:
    return datetime(*args, tzinfo=tz)


def test_fields_and_steps():
    cron = Cron.parse("*/15 9-17 * * 1-5")
    assert cron.next_after(_at(2026, 10, 12, 8, 59), LONDON) == _at(2026, 10, 12, 9, 0)
    assert cron.next_after(_at(2026, 10, 12, 9, 0), LONDON) == _at(2026, 10, 12, 9, 15)
    assert cron.next_after(_at(2026, 10, 16, 17, 45), LONDON) == _at(2026, 10, 19, 9, 0)
    lists = Cron.parse("5,35 0-23/6 1 1,7 *")
    assert lists.next_after(_at(2026, 1, 1, 0, 6), LONDON) == _at(2026, 1, 1, 0, 35)
    assert lists.next_after(_at(2026, 1, 1, 18, 35), LONDON) == _at(2026, 7, 1, 0, 5)


def test_dow_7_is_sunday():
    sunday = _at(2026, 10, 18, 12, 0)
    for expr in ("0 12 * * 7", "0 12 * * 0"):
        assert Cron.parse(expr).next_after(_at(2026, 10, 12, 0, 0), LONDON) == sunday


def test_dom_or_dow():
    cron = Cron.parse("0 0 13 * 5")  # the 13th, or any Friday
    assert cron.next_after(_at(2026, 10, 12, 0, 0), LONDON) == _at(2026, 10, 13, 0, 0)
    assert cron.next_after(_at(2026, 10, 13, 0, 0), LONDON) == _at(2026, 10, 16, 0, 0)
    starred = Cron.parse("0 0 */2 * 5")  # a day field that starts with * means both must match
    assert starred.next_after(_at(2026, 10, 1, 0, 0), LONDON) == _at(2026, 10, 9, 0, 0)


def test_spring_forward_skips_the_missing_time():
    cron = Cron.parse("30 1 * * *")
    due = cron.next_after(_at(2026, 3, 28, 2, 0), LONDON)
    assert due == _at(2026, 3, 30, 1, 30)  # 01:30 on 29 March never happened in London


def test_fall_back_fires_once():
    cron = Cron.parse("30 1 * * *")
    first = cron.next_after(_at(2026, 10, 24, 12, 0), LONDON)
    assert first.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 01:30 BST
    second = cron.next_after(first, LONDON)
    assert second.astimezone(UTC) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)  # 01:30 GMT


def test_next_is_strictly_after_and_in_the_zone():
    cron = Cron.parse("0 * * * *")
    due = cron.next_after(datetime(2026, 10, 12, 9, 0, tzinfo=UTC), LONDON)
    assert due.tzinfo is LONDON and due.astimezone(UTC) == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "expr",
    [
        "61 * * * *",
        "* * *",
        "*/0 * * * *",
        "* * * * * *",
        "5-1 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "x * * * *",
        "1,,2 * * * *",
        "",
    ],
)
def test_bad_expressions_raise(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_a_time_that_never_comes_raises():
    with pytest.raises(CronError, match="never"):
        Cron.parse("0 0 31 2 *").next_after(_at(2026, 1, 1, 0, 0), LONDON)


@pytest.mark.parametrize("expr", [5, None, ["0 9 * * *"], {"minute": 0}])
def test_a_cron_that_is_not_text_is_refused(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


@pytest.mark.parametrize("name", [5, None, ["UTC"]])
def test_a_zone_that_is_not_text_is_refused(name):
    with pytest.raises(CronError):
        zone(name)
