"""Calendar primitives: truncate, shift, spine, and fiscal alignment.

What this file provides
    Unit tests for kpi_engine.dates — the module every shift and window
    depends on. Leap years, quarter/year steps, fiscal starts, reversed
    ranges, and parse failures.

Where it is used
    pytest tests/test_dates_unit.py — pure Python, no DuckDB.

When to use
    Add a case before changing any period math; these are the invariants the
    calculation layer assumes.
"""

from datetime import date, datetime

import pytest

from kpi_engine.contracts import Offset, TimeSpec
from kpi_engine.dates import (
    add_days,
    add_months,
    add_periods,
    apply_offset,
    iso_month,
    iso_period,
    month_range_inclusive,
    parse_date,
    parse_month,
    period_range_inclusive,
    periods_between,
    truncate_period,
)
from kpi_engine.exceptions import TimePlanError


def _spec(grain: str, calendar: str = "gregorian", start_month: int = 4) -> TimeSpec:
    """TimeSpec for a grain/calendar combination."""
    return TimeSpec(
        column="event_month",
        grain=grain,
        filter_code="reporting_month",
        calendar=calendar,
        fiscal_start_month=start_month,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03", date(2026, 3, 1)),
        ("2026-03-17", date(2026, 3, 17)),
        (" 2026-03-17 ", date(2026, 3, 17)),
        (date(2026, 3, 17), date(2026, 3, 17)),
        (datetime(2026, 3, 17, 23, 59), date(2026, 3, 17)),
    ],
)
def test_parse_date_accepts_context_formats(value, expected):
    """Filter values arrive as YYYY-MM, YYYY-MM-DD, date, or datetime."""
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["", "March", "2026", "2026-13", "20260317", None])
def test_parse_date_rejects_unparseable_values(value):
    """An unreadable selected period fails loudly instead of defaulting."""
    with pytest.raises(TimePlanError, match="Cannot parse date"):
        parse_date(value)


def test_parse_month_drops_the_day():
    """Month anchors always normalize to the first of the month."""
    assert parse_month("2026-03-31") == date(2026, 3, 1)
    assert parse_month(datetime(2026, 3, 31, 12)) == date(2026, 3, 1)


@pytest.mark.parametrize(
    ("grain", "calendar", "value", "expected"),
    [
        ("day", "gregorian", date(2026, 3, 17), date(2026, 3, 17)),
        ("month", "gregorian", date(2026, 3, 17), date(2026, 3, 1)),
        ("quarter", "gregorian", date(2026, 3, 17), date(2026, 1, 1)),
        ("quarter", "gregorian", date(2026, 5, 17), date(2026, 4, 1)),
        ("quarter", "gregorian", date(2026, 12, 31), date(2026, 10, 1)),
        ("year", "gregorian", date(2026, 3, 17), date(2026, 1, 1)),
        ("year", "fiscal", date(2026, 3, 17), date(2025, 4, 1)),
        ("year", "fiscal", date(2026, 4, 1), date(2026, 4, 1)),
        ("quarter", "fiscal", date(2026, 3, 17), date(2026, 1, 1)),
        ("quarter", "fiscal", date(2026, 4, 2), date(2026, 4, 1)),
        ("quarter", "fiscal", date(2026, 9, 30), date(2026, 7, 1)),
    ],
)
def test_truncate_period_by_grain_and_calendar(grain, calendar, value, expected):
    """Each grain lands on the first day of the containing period."""
    assert truncate_period(value, _spec(grain, calendar)) == expected


def test_fiscal_year_start_month_shifts_the_whole_calendar():
    """A July fiscal year puts Jan-Mar in Q3 of the previous fiscal year."""
    july = _spec("quarter", "fiscal", start_month=7)
    assert truncate_period(date(2026, 3, 15), july) == date(2026, 1, 1)
    assert truncate_period(date(2026, 7, 1), july) == date(2026, 7, 1)
    assert truncate_period(date(2026, 6, 30), _spec("year", "fiscal", 7)) == date(2025, 7, 1)


def test_unknown_grain_is_rejected():
    """A TimeSpec built outside the binder still cannot use an unknown grain."""
    with pytest.raises(TimePlanError, match="Unknown time.grain"):
        truncate_period(date(2026, 3, 1), _spec("week"))
    with pytest.raises(TimePlanError, match="Unknown time.grain"):
        add_periods(date(2026, 3, 1), 1, _spec("week"))


def test_invalid_fiscal_start_month_is_rejected_by_the_calendar():
    """dates guards fiscal_start_month even if a TimeSpec is built by hand."""
    with pytest.raises(TimePlanError, match="fiscal_start_month must be 1-12"):
        truncate_period(date(2026, 3, 1), _spec("year", "fiscal", start_month=0))


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 3, 1), 1, date(2026, 4, 1)),
        (date(2026, 12, 1), 1, date(2027, 1, 1)),
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
        (date(2026, 3, 1), -12, date(2025, 3, 1)),
        (date(2026, 3, 1), 0, date(2026, 3, 1)),
    ],
)
def test_add_months_is_calendar_arithmetic(start, months, expected):
    """Month shifts cross year boundaries in both directions."""
    assert add_months(start, months) == expected


@pytest.mark.parametrize(
    ("grain", "periods", "expected"),
    [
        ("day", 1, date(2026, 3, 2)),
        ("day", -1, date(2026, 2, 28)),
        ("month", 1, date(2026, 4, 1)),
        ("quarter", 1, date(2026, 4, 1)),
        ("quarter", -1, date(2025, 10, 1)),
        ("year", 1, date(2027, 1, 1)),
        ("year", -2, date(2024, 1, 1)),
    ],
)
def test_add_periods_steps_by_grain(grain, periods, expected):
    """One "period" means one grain step, not 30 days."""
    assert add_periods(date(2026, 3, 1), periods, _spec(grain)) == expected


def test_add_periods_on_fiscal_year_stays_on_the_fiscal_start():
    """Stepping a fiscal year keeps the April boundary."""
    spec = _spec("year", "fiscal")
    assert add_periods(date(2026, 3, 1), 1, spec) == date(2026, 4, 1)
    assert add_periods(date(2026, 3, 1), -1, spec) == date(2024, 4, 1)


def test_add_days_crosses_month_and_leap_day():
    """Day math uses the real calendar, including 29 February."""
    assert add_days(date(2024, 2, 28), 1) == date(2024, 2, 29)
    assert add_days(date(2023, 2, 28), 1) == date(2023, 3, 1)
    assert add_days(date(2026, 3, 1), -1) == date(2026, 2, 28)


def test_apply_offset_combines_units_and_clamps_month_end():
    """Offsets add days, months, quarters, and years, clamping to a valid day."""
    assert apply_offset(date(2026, 3, 31), Offset(months=1)) == date(2026, 4, 30)
    assert apply_offset(date(2024, 2, 29), Offset(years=1)) == date(2025, 2, 28)
    assert apply_offset(date(2024, 2, 29), Offset(years=4)) == date(2028, 2, 29)
    assert apply_offset(date(2026, 3, 15), Offset(quarters=2)) == date(2026, 9, 15)
    assert apply_offset(date(2026, 3, 15), Offset(days=-20)) == date(2026, 2, 23)
    assert apply_offset(date(2026, 3, 15), Offset(years=1, months=2)) == date(2027, 5, 15)


def test_apply_offset_without_an_offset_is_identity():
    """A measure with no offset points at the anchor itself."""
    assert apply_offset(date(2026, 3, 15), None) == date(2026, 3, 15)
    assert apply_offset("2026-03-15", Offset()) == date(2026, 3, 15)


def test_month_range_is_inclusive_and_dense():
    """The spine includes both ends and every month between."""
    months = month_range_inclusive(date(2025, 11, 1), date(2026, 2, 1))
    assert months == [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]
    assert month_range_inclusive(date(2026, 3, 1), date(2026, 3, 1)) == [date(2026, 3, 1)]


def test_reversed_ranges_are_empty_not_infinite():
    """A backwards span yields no periods rather than looping forever."""
    assert month_range_inclusive(date(2026, 3, 1), date(2026, 1, 1)) == []
    assert period_range_inclusive(date(2026, 3, 1), date(2026, 1, 1), _spec("month")) == []


@pytest.mark.parametrize(
    ("grain", "start", "end", "expected"),
    [
        ("day", date(2026, 2, 27), date(2026, 3, 2), 4),
        ("month", date(2026, 1, 1), date(2026, 3, 1), 3),
        ("quarter", date(2026, 1, 1), date(2026, 12, 31), 4),
        ("year", date(2024, 1, 1), date(2026, 1, 1), 3),
    ],
)
def test_period_range_length_matches_grain(grain, start, end, expected):
    """Trend axes get one slot per grain period in the window."""
    assert len(period_range_inclusive(start, end, _spec(grain))) == expected


def test_periods_between_counts_steps_and_ignores_direction():
    """Distance is symmetric and zero for the same period."""
    spec = _spec("month")
    assert periods_between(date(2025, 3, 1), date(2026, 3, 1), spec) == 12
    assert periods_between(date(2026, 3, 1), date(2025, 3, 1), spec) == 12
    assert periods_between(date(2026, 3, 1), date(2026, 3, 28), spec) == 0
    assert periods_between(date(2025, 1, 1), date(2026, 1, 1), _spec("quarter")) == 4


def test_iso_period_formats_by_grain():
    """JSON periods are ISO first-of-period dates; day grain keeps the day."""
    assert iso_month(date(2026, 3, 17)) == "2026-03-01"
    assert iso_period(date(2026, 3, 17)) == "2026-03-01"
    assert iso_period(date(2026, 3, 17), _spec("day")) == "2026-03-17"
    assert iso_period(date(2026, 3, 17), _spec("quarter")) == "2026-01-01"
    assert iso_period(date(2026, 3, 17), _spec("year", "fiscal")) == "2025-04-01"
