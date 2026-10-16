"""Five-field cron, read the way Vixie cron reads it, in a routine's own time zone.

Fields: minute, hour, day of month, month, day of week. Each is ``*``, a
number, a range ``a-b``, a step ``*/n`` or ``a-b/n``, or a list of those.
Day of week runs 0 to 7, and both 0 and 7 are Sunday. When both day fields
are restricted, a match on either comes due; when either starts with ``*``,
both must match.

Times follow the wall clock of the zone. A wall time the clocks skip in
spring never comes due that day, and a wall time the clocks repeat in autumn
comes due once, the first time.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12),
          ("day of week", 0, 7))  # fmt: skip
SEARCH_YEARS = 5
_PART = re.compile(r"^(\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")


class CronError(ValueError):
    pass


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        match = _PART.match(part)
        if match is None:
            raise CronError(f"{name}: {part!r} is not *, n, a-b, */n or a-b/n")
        star, start, end, step = match.groups()
        if star == "*":
            first, last = low, high
        else:
            first = int(start)
            last = int(end) if end is not None else (high if step else first)
        if step is not None and int(step) == 0:
            raise CronError(f"{name}: a step of 0")
        if not (low <= first <= high and low <= last <= high):
            raise CronError(f"{name}: {part} is outside {low}-{high}")
        if first > last:
            raise CronError(f"{name}: {part} runs backwards")
        values.update(range(first, last + 1, int(step) if step else 1))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    day_star: bool
    weekday_star: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        if expr is not None and not isinstance(expr, str):  # a file edited by hand
            raise CronError("a cron expression is text")
        parts = (expr or "").split()
        if len(parts) != len(FIELDS):
            raise CronError("a cron expression has five fields: minute hour day month weekday")
        sets = [_field(p, *spec) for p, spec in zip(parts, FIELDS, strict=True)]
        weekdays = frozenset(0 if d == 7 else d for d in sets[4])
        return cls(
            " ".join(parts), sets[0], sets[1], sets[2], sets[3], weekdays,
            parts[2].startswith("*"), parts[4].startswith("*"),
        )  # fmt: skip

    def day_matches(self, day: datetime) -> bool:
        in_month = day.day in self.days
        in_week = (day.isoweekday() % 7) in self.weekdays
        if self.day_star or self.weekday_star:
            return in_month and in_week
        return in_month or in_week

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime:
        """The first due time strictly after ``after``, as an aware time in ``tz``."""
        if after.tzinfo is None:
            raise CronError("after must carry its time zone")
        instant = after.astimezone(UTC)
        wall = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
        wall += timedelta(minutes=1)
        limit = wall + timedelta(days=366 * SEARCH_YEARS)
        while wall < limit:
            if wall.month not in self.months:
                year, month = (
                    (wall.year + 1, 1) if wall.month == 12 else (wall.year, wall.month + 1)
                )
                wall = datetime(year, month, 1)
            elif not self.day_matches(wall):
                wall = datetime(wall.year, wall.month, wall.day) + timedelta(days=1)
            elif wall.hour not in self.hours:
                wall = wall.replace(minute=0) + timedelta(hours=1)
            elif wall.minute not in self.minutes:
                wall += timedelta(minutes=1)
            else:
                due = _on_the_clock(wall, tz)
                if due is not None and due.astimezone(UTC) > instant:
                    return due
                wall += timedelta(minutes=1)
        raise CronError(f"{self.expr} never comes due")


def _on_the_clock(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """``wall`` in ``tz``, its first occurrence; None for a time the clocks skipped."""
    due = wall.replace(tzinfo=tz, fold=0)
    back = due.astimezone(UTC).astimezone(tz)
    return due if back.replace(tzinfo=None) == wall else None


def zone(name: str) -> ZoneInfo:
    if not isinstance(name, str):  # a file edited by hand
        raise CronError(f"unknown time zone {name!r}")
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError) as exc:  # not found is a KeyError
        raise CronError(f"unknown time zone {name!r}") from exc


def local_zone_name() -> str:
    """This machine's zone: ``TZ``, then ``/etc/timezone``, then the ``/etc/localtime`` link.

    A name that is not a zone is passed over; with none left, UTC.
    """
    names = [os.environ.get("TZ", "").removeprefix(":")]
    with contextlib.suppress(OSError):
        names.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    with contextlib.suppress(OSError):
        _, found, name = os.readlink("/etc/localtime").partition("zoneinfo/")
        names.append(name if found else "")
    for name in names:
        if not name or name.startswith("/"):
            continue
        with contextlib.suppress(CronError):
            zone(name)
            return name
    return "UTC"
