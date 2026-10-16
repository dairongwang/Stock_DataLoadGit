"""
US stock market trading calendar.

Used by the *missing market day* data quality check: a stock that has no
valid row for a day on which the market was open is an alert-level data
error.  The calendar knows weekends and the official NYSE/NASDAQ holidays
(New Year's Day, Martin Luther King Jr. Day, Washington's Birthday,
Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
Thanksgiving and Christmas) including the Saturday->Friday /
Sunday->Monday observance rules.

Extra (ad-hoc) closures - e.g. days of mourning - can be configured in
``config.ini`` (``[quality] extra_holidays``) and are passed to
:func:`is_market_open` by the caller.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Collection, FrozenSet

__all__ = ["easter_sunday", "us_market_holidays", "is_market_open", "parse_holiday"]


class CalendarError(ValueError):
    """A date could not be interpreted."""


def easter_sunday(year: int) -> date:
    """Easter Sunday (Gregorian) - anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) *weekday* (0=Monday) of *month*."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(holiday: date) -> date | None:
    """NYSE observance: Saturday -> preceding Friday, Sunday -> following Monday.

    If the observed day would fall in the previous year (e.g. New Year's Day
    on a Saturday) the exchange does not observe it - return ``None``.
    """
    if holiday.weekday() == 5:  # Saturday
        observed = holiday - timedelta(days=1)
        return None if observed.year != holiday.year else observed
    if holiday.weekday() == 6:  # Sunday
        return holiday + timedelta(days=1)
    return holiday


def us_market_holidays(year: int) -> FrozenSet[date]:
    """All full-day US market holidays (observed dates) of *year*."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)]
    if year >= 2022:  # Juneteenth became a market holiday in 2022
        fixed.append(date(year, 6, 19))
    floating = [
        _nth_weekday(year, 1, 0, 3),    # Martin Luther King Jr. Day (3rd Monday Jan)
        _nth_weekday(year, 2, 0, 3),    # Washington's Birthday (3rd Monday Feb)
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),      # Memorial Day (last Monday May)
        _nth_weekday(year, 9, 0, 1),    # Labor Day (1st Monday Sep)
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving (4th Thursday Nov)
    ]
    holidays = set(floating)
    for day in fixed:
        observed = _observed(day)
        if observed is not None:
            holidays.add(observed)
    # New Year's Day of *next* year observed on Dec 31 of this year? Not on NYSE
    # (rule 7.2: no observance when it would move into the prior year) - nothing to add.
    return frozenset(holidays)


def parse_holiday(value: str | date) -> date:
    """Parse ``YYYY-MM-DD`` / ``YYYYMMDD`` (or pass a date through)."""
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise CalendarError(f"cannot parse holiday date '{value}' (use YYYY-MM-DD or YYYYMMDD)")


def is_market_open(day: date, extra_holidays: Collection[date] = ()) -> bool:
    """True if the US stock market is open on *day*."""
    if day.weekday() >= 5:  # Saturday / Sunday
        return False
    if day in us_market_holidays(day.year):
        return False
    return day not in set(extra_holidays)
