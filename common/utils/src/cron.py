"""Standard crontab schedules on APScheduler 3.x (routines, stories 2.1–2.2).

``CronTrigger.from_crontab`` in APScheduler 3.x reads a numeric day of week
with **0 = Monday**; a standard crontab reads it with **0 = Sunday** (its own
docstring calls this "a historical mistake", fixed only in 4.x). Passed
through unchanged, ``0 9 * * 1`` — "Mondays at 09:00" to anyone who writes
cron — fires on Tuesdays, and ``*/2`` picks different days.

So the day-of-week field is translated here from standard crontab to day
*names*, which both read the same way, and every service that saves or
fires a routine goes through this one module: the Gateway validates and
computes the first slot with it, the SuperAgent's scheduler every later
slot. One cron dialect, one translation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Standard crontab day numbers; 7 is Sunday too.
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NAME_TO_NUMBER = {name: i for i, name in enumerate(_DAY_NAMES)}


def _day_number(token: str) -> int:
    """0–7 as written (7 kept, so ``5-7`` is a valid range), or a day name."""
    token = token.strip().lower()
    if token in _NAME_TO_NUMBER:
        return _NAME_TO_NUMBER[token]
    if not token.isdigit():
        raise ValueError(f"invalid day of week: {token!r}")
    number = int(token)
    if not 0 <= number <= 7:
        raise ValueError(f"day of week out of range: {number}")
    return number


def standard_day_of_week(field: str) -> str:
    """A standard crontab day-of-week field as APScheduler day names.

    ``*`` stays ``*``. Anything else — numbers, names, ranges, lists and
    steps — is expanded to the days it selects in standard cron and written
    as names (``1-5`` → ``mon,tue,wed,thu,fri``; ``*/2`` → ``sun,tue,thu,sat``).
    Raises ``ValueError`` for a field that does not parse.
    """
    field = field.strip()
    if field == "*":
        return "*"
    days: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"invalid step: {part!r}")
            step = int(step_text)
        if base == "*":
            start, end = 0, 6
        elif "-" in base:
            low, high = base.split("-", 1)
            start, end = _day_number(low), _day_number(high)
            if end < start:
                raise ValueError(f"invalid day range: {base!r}")
        else:
            if step_text:
                raise ValueError(f"a step needs a range: {part!r}")
            days.add(_day_number(base) % 7)
            continue
        days.update(d % 7 for d in range(start, end + 1, step))
    if not days:
        raise ValueError(f"no days selected: {field!r}")
    return ",".join(_DAY_NAMES[d] for d in sorted(days))


def crontab_trigger(expr: str, tz: str) -> Any:
    """An APScheduler ``CronTrigger`` that reads ``expr`` as a standard crontab.

    Raises ``ValueError`` for an expression or time zone that does not parse.
    """
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
    try:
        zone = ZoneInfo(tz)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError
        raise ValueError(f"unknown time zone: {tz}") from exc
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=standard_day_of_week(day_of_week),
        timezone=zone,
    )


def next_slot(expr: str, tz: str, after: datetime) -> datetime:
    """The first slot of ``expr`` in ``tz`` strictly after ``after``, in UTC."""
    trigger = crontab_trigger(expr, tz)
    # CronTrigger returns the first fire time at or after ``now``, at whole-
    # second resolution; one second past ``after`` makes it strictly after.
    found = trigger.get_next_fire_time(
        None, after.astimezone(UTC) + timedelta(seconds=1)
    )
    if found is None:
        raise ValueError(f"cron {expr!r} has no future slot")
    return found.astimezone(UTC)
