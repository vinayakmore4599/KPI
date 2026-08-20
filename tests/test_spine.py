"""Spine tests: calendar shifts and trend slot length.

What this file provides
    previous_year_value on a missing month is null (not 12 rows back).
    trend_12m stays length 12 and is omitted on R when cuts: [G].

Where it is used
    pytest tests/test_spine.py (runs DuckDB + Pandas on local parquet).

When to use
    Add cases for other offsets (3m/6m point) if those land on sparse series.
"""
from kpi_engine import compute
from tests.conftest import make_context


def _g_late(result: dict) -> dict:
    """Global-cut row for LATE_SUPPLIER."""
    matches = [
        r
        for r in result["rows"]
        if r["output_cut"] == "G" and r["reason_code"] == "LATE_SUPPLIER"
    ]
    assert matches, result["rows"]
    return matches[0]


def test_previous_year_uses_calendar_month_not_row_shift(parquet_path, config_dir):
    """A missing month is null for previous_year_value, not whatever sat 12 rows back."""
    ctx = make_context(
        parquet_path,
        measures=["previous_year_value", "current_value", "reason_code"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    global_row = _g_late(result)
    # NA LATE_SUPPLIER has no 2025-03 row; EU LATE_SUPPLIER does (5 * 3 = 15).
    # Point lookup is calendar-based: missing NA month is null, not a shifted row.
    # G sums observed partitions only for the point (null + 15).
    na = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "R"
        and r["reason_code"] == "LATE_SUPPLIER"
        and r["region"] == "NA"
    )
    eu = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "R"
        and r["reason_code"] == "LATE_SUPPLIER"
        and r["region"] == "EU"
    )
    assert na["previous_year_value"] is None
    assert eu["previous_year_value"] == 15.0
    assert global_row["previous_year_value"] == 15.0
    assert na["current_value"] == 10.0 * 3
    assert "IN (" not in result["sql"] or '"event_month" IN' not in result["sql"]


def test_trend_keeps_slot_for_missing_month(parquet_path, config_dir):
    """Trend arrays stay 12 long and are omitted on cuts not listed in measures.cuts."""
    ctx = make_context(parquet_path, measures=["trend_12m"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    axis = result["trend_axes"]["trend_12m"]
    assert len(axis) == 12
    assert axis[0] == "2025-04-01"
    assert axis[-1] == "2026-03-01"
    row = _g_late(result)
    assert len(row["trend_12m"]) == 12
    r_rows = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert all("trend_12m" not in r for r in r_rows)
