"""Calendar helpers for month-grain KPIs (gregorian, first-of-month DATE).

What this file provides
    parse_month — `2026-08` or `2026-08-01` → date(2026, 8, 1)
    add_months — calendar shift (used for lookbacks and point offsets)
    month_range_inclusive — dense month list for the spine and trend axes
    iso_month — JSON metadata format YYYY-MM-01

Where it is used
    time_planner (anchor and required_span), calc_engine (point/window/trend),
    orchestrator (response parameters).

Capabilities
    All period math is calendar months, never "N rows back".

When to use
    Any time you need a month bucket. Do not use datetime.today() or
    context.business_date — the user-selected month is the only clock.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from kpi_engine.exceptions import TimePlanError


def parse_month(value: Any) -> date:
    """Normalize `2026-08` or `2026-08-01` (or date/datetime) to first of month."""
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    text = str(value).strip()
    if len(text) >= 7 and text[4] == "-":
        try:
            year = int(text[0:4])
            month = int(text[5:7])
            return date(year, month, 1)
        except ValueError:
            pass
    raise TimePlanError(
        f"Cannot parse month {value!r}. Expected YYYY-MM or YYYY-MM-DD."
    )


def add_months(anchor: date, months: int) -> date:
    """Shift a first-of-month DATE by N calendar months (negative is lookback)."""
    month0 = anchor.month - 1 + months
    year = anchor.year + month0 // 12
    month = month0 % 12 + 1
    return date(year, month, 1)


def month_range_inclusive(start: date, end: date) -> list[date]:
    """List first-of-month dates from start through end, inclusive."""
    start = parse_month(start)
    end = parse_month(end)
    if start > end:
        return []
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor = add_months(cursor, 1)
    return out


def iso_month(value: date) -> str:
    """Format a period as YYYY-MM-01 for JSON metadata."""
    d = parse_month(value)
    return f"{d.year:04d}-{d.month:02d}-01"
