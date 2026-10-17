"""Clock abstraction and NYSE trading calendar.

The engine never calls ``datetime.now()`` directly but goes through a :data:`Clock`, so tests can
freeze or advance time deterministically.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from functools import lru_cache
from zoneinfo import ZoneInfo

type Clock = Callable[[], dt.datetime]
"""Returns the current timezone-aware time."""


def system_clock(tz: ZoneInfo) -> Clock:
    """Clock backed by the system time in ``tz``."""

    def now() -> dt.datetime:
        return dt.datetime.now(tz)

    return now


def at(day: dt.date, time: dt.time, tz: ZoneInfo) -> dt.datetime:
    """Combine a date and a wall-clock time in ``tz``."""
    return dt.datetime.combine(day, time, tzinfo=tz)


def _easter(year: int) -> dt.date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """``n``-th ``weekday`` (Mon=0) of a month; ``n=-1`` is the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """NYSE observance: Saturday holidays move to Friday, Sunday holidays to Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[dt.date]:
    """Full-day NYSE holidays of ``year`` (regular rules, no ad-hoc closures)."""
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(dt.date(year, 6, 19)),  # Juneteenth
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(dt.date(year, 12, 25)),  # Christmas
    }
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:  # NYSE does not observe a Saturday New Year on Dec 31
        days.add(_observed(new_year))
    return frozenset(days)


def is_trading_day(day: dt.date) -> bool:
    """Whether the NYSE is open on ``day``."""
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def previous_trading_day(day: dt.date) -> dt.date:
    """Last trading day strictly before ``day``."""
    day -= dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day
