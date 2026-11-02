"""Preset date ranges, resolved to half-open ``[start, end)`` day ranges.

Weeks start on Monday (ISO 8601). "This" periods run to the end of the period, so they include
later days of the current week/month/year. ``last_6_months`` is the current month plus the five
before it, so month buckets line up with calendar months.
"""

from __future__ import annotations

from datetime import date, timedelta

LABELS = {
    "today": "Today", "last_7_days": "Last 7 days", "last_30_days": "Last 30 days",
    "last_90_days": "Last 90 days", "this_week": "This week", "last_week": "Last week",
    "this_month": "This month", "last_month": "Last month", "last_6_months": "Last 6 months",
    "this_year": "This year", "last_year": "Last year",
}


def _month_start(d: date, back: int = 0) -> date:
    total = d.year * 12 + (d.month - 1) - back
    return date(total // 12, total % 12 + 1, 1)


def resolve_period(name: str, today: date) -> tuple[date, date]:
    if name == "today":
        return today, today + timedelta(days=1)
    if name in ("last_7_days", "last_30_days", "last_90_days"):
        days = int(name.split("_")[1])  # rolling window ending today, inclusive
        return today - timedelta(days=days - 1), today + timedelta(days=1)
    if name in ("this_week", "last_week"):
        monday = today - timedelta(days=today.weekday())
        if name == "last_week":
            monday -= timedelta(days=7)
        return monday, monday + timedelta(days=7)
    if name == "this_month":
        return _month_start(today), _month_start(today, -1)
    if name == "last_month":
        return _month_start(today, 1), _month_start(today)
    if name == "last_6_months":
        return _month_start(today, 5), _month_start(today, -1)
    if name == "this_year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    if name == "last_year":
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    raise ValueError(f"unknown period {name!r}")


def bucket_starts(grain: str, start: date, end: date) -> list[date]:
    """Every bucket start of ``grain`` overlapping ``[start, end)``."""
    out: list[date] = []
    if grain == "day":
        d = start
        while d < end:
            out.append(d)
            d += timedelta(days=1)
    elif grain == "month":
        d = _month_start(start)
        while d < end:
            out.append(d)
            d = _month_start(d, -1)
    else:
        out = [date(y, 1, 1) for y in range(start.year, (end - timedelta(days=1)).year + 1)]
    return out
