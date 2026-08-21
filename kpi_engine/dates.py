"""Calendar helpers: period truncate, offsets, dense spines (gregorian and fiscal).

What this file provides
    parse_month / parse_date — context filter values to DATE
    truncate_period — first day of day/month/quarter/year (gregorian or fiscal)
    add_months / add_periods / apply_offset — calendar shifts, never row offsets
    period_range_inclusive / month_range_inclusive — dense spines
    iso_month / iso_period — JSON metadata

Where it is used
    time_planner, calc_engine, orchestrator, model_sql (fiscal SQL uses the
    same start-month rule).

Capabilities
    Default calendar is gregorian. Fiscal uses time.fiscal_start_month (April=4).

When to use
    Any period math. Do not use datetime.today() or context.business_date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from kpi_engine.contracts import Offset, TimeSpec
from kpi_engine.exceptions import TimePlanError


def parse_month(value: Any) -> date:
    """Normalize `2026-08` or `2026-08-01` (or date/datetime) to first of month."""
    parsed = parse_date(value)
    return date(parsed.year, parsed.month, 1)


def parse_date(value: Any) -> date:
    """Parse YYYY-MM, YYYY-MM-DD, date, or datetime to a DATE (day kept when present)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        except ValueError:
            pass
    if len(text) >= 7 and text[4] == "-":
        try:
            return date(int(text[0:4]), int(text[5:7]), 1)
        except ValueError:
            pass
    raise TimePlanError(
        f"Cannot parse date {value!r}. Expected YYYY-MM or YYYY-MM-DD."
    )


def truncate_period(value: date, time: TimeSpec) -> date:
    """First day of the period that contains `value` for this KPI's grain/calendar."""
    day = parse_date(value)
    grain = time.grain
    if grain == "day":
        return day
    if grain == "month":
        return date(day.year, day.month, 1)
    if grain == "year":
        if time.calendar == "fiscal":
            return _fiscal_year_start(day, time.fiscal_start_month)
        return date(day.year, 1, 1)
    if grain == "quarter":
        if time.calendar == "fiscal":
            return _fiscal_quarter_start(day, time.fiscal_start_month)
        month = ((day.month - 1) // 3) * 3 + 1
        return date(day.year, month, 1)
    raise TimePlanError(f"Unknown time.grain {grain!r}.")


def add_months(anchor: date, months: int) -> date:
    """Shift a first-of-month DATE by N calendar months (negative is lookback)."""
    month0 = anchor.month - 1 + months
    year = anchor.year + month0 // 12
    month = month0 % 12 + 1
    return date(year, month, 1)


def add_days(anchor: date, days: int) -> date:
    """Shift a DATE by N calendar days."""
    return parse_date(anchor) + timedelta(days=days)


def add_periods(anchor: date, periods: int, time: TimeSpec) -> date:
    """Shift a truncated period start by N grain periods, then re-truncate."""
    start = truncate_period(anchor, time)
    if time.grain == "day":
        return add_days(start, periods)
    if time.grain == "month":
        return add_months(start, periods)
    if time.grain == "quarter":
        return truncate_period(add_months(start, periods * 3), time)
    if time.grain == "year":
        return truncate_period(add_months(start, periods * 12), time)
    raise TimePlanError(f"Unknown time.grain {time.grain!r}.")


def apply_offset(anchor: date, offset: Offset | None) -> date:
    """Apply day/month/quarter/year offset as calendar math (keeps day of month)."""
    if offset is None:
        return parse_date(anchor)
    d = parse_date(anchor) + timedelta(days=offset.days)
    month0 = d.month - 1 + offset.months + offset.years * 12 + offset.quarters * 3
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last))


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


def period_range_inclusive(start: date, end: date, time: TimeSpec) -> list[date]:
    """Dense period starts from start through end at the KPI grain."""
    start = truncate_period(start, time)
    end = truncate_period(end, time)
    if start > end:
        return []
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor = add_periods(cursor, 1, time)
    return out


def periods_between(start: date, end: date, time: TimeSpec) -> int:
    """How many grain steps from start to end (0 if equal). End must be >= start."""
    start = truncate_period(start, time)
    end = truncate_period(end, time)
    if start > end:
        start, end = end, start
    n = 0
    cursor = start
    while cursor < end:
        cursor = add_periods(cursor, 1, time)
        n += 1
        if n > 100_000:
            raise TimePlanError("Period distance overflow.")
    return n


def iso_month(value: date) -> str:
    """Format a period as YYYY-MM-01 for JSON metadata."""
    d = parse_month(value)
    return f"{d.year:04d}-{d.month:02d}-01"


def iso_period(value: date, time: TimeSpec | None = None) -> str:
    """JSON period: YYYY-MM-DD for day grain, otherwise first-of-period YYYY-MM-DD."""
    if time is None or time.grain == "month":
        return iso_month(value)
    d = truncate_period(value, time)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _fiscal_year_start(day: date, start_month: int) -> date:
    """First day of the fiscal year containing `day` (start_month is 1-12)."""
    start_month = _fiscal_month(start_month)
    if day.month >= start_month:
        return date(day.year, start_month, 1)
    return date(day.year - 1, start_month, 1)


def _fiscal_quarter_start(day: date, start_month: int) -> date:
    """First day of the fiscal quarter containing `day`."""
    start_month = _fiscal_month(start_month)
    fy = _fiscal_year_start(day, start_month)
    months_in = (day.year - fy.year) * 12 + (day.month - fy.month)
    q = months_in // 3
    return add_months(fy, q * 3)


def _fiscal_month(start_month: int) -> int:
    """Validate fiscal year start month."""
    if start_month < 1 or start_month > 12:
        raise TimePlanError(f"fiscal_start_month must be 1-12 (got {start_month}).")
    return start_month
