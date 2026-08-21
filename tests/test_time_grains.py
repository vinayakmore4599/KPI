"""Day, quarter, and fiscal calendar tests.

What this file provides
    grain=day trailing windows, gregorian quarter buckets, fiscal year/quarter
    truncate (fiscal_start_month), and lookback in grain periods not months.

Where it is used
    pytest tests/test_time_grains.py.

When to use
    Add a case when a KPI uses a non-month grain or calendar: fiscal.
"""

from datetime import date

import pandas as pd

from kpi_engine import compute, validate
from kpi_engine.contracts import TimeSpec
from kpi_engine.dates import iso_period, truncate_period
from tests.conftest import make_context, write_yaml


def test_fiscal_year_and_quarter_truncate():
    """Fiscal start in May: March sits in the Feb–Apr quarter of the prior FY."""
    fiscal = TimeSpec(
        column="event_month",
        grain="quarter",
        filter_code="reporting_month",
        calendar="fiscal",
        fiscal_start_month=5,
    )
    assert truncate_period(date(2026, 3, 15), fiscal) == date(2026, 2, 1)
    year = TimeSpec(
        column="event_month",
        grain="year",
        filter_code="reporting_month",
        calendar="fiscal",
        fiscal_start_month=5,
    )
    assert truncate_period(date(2026, 3, 15), year) == date(2025, 5, 1)
    greg_q = TimeSpec(
        column="event_month",
        grain="quarter",
        filter_code="reporting_month",
        calendar="gregorian",
    )
    assert truncate_period(date(2026, 3, 15), greg_q) == date(2026, 1, 1)
    assert iso_period(date(2026, 3, 15), fiscal) == "2026-02-01"


def test_day_grain_trailing_window(tmp_path, extra_config):
    """Inclusive 3-day window at 2026-03-15 is the 13th–15th, not three months."""
    path = tmp_path / "daily.parquet"
    pd.DataFrame(
        [
            {"event_month": "2026-03-13", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 1},
            {"event_month": "2026-03-14", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 2},
            {"event_month": "2026-03-15", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 4},
            {"event_month": "2026-03-12", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 100},
        ]
    ).to_parquet(path, index=False)
    write_yaml(extra_config / "kpis" / "9032.yaml", _grain_kpi(9032, grain="day"))
    ctx = make_context(
        path,
        measures=["current_value", "value_3p"],
        supplier=["ABC"],
        kpi_id=9032,
        month="2026-03-15",
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["anchor"] == "2026-03-15"
    assert planned["lookback_months"] == 2
    assert planned["span_start"] == "2026-03-13"
    sql = planned["sql"]
    assert "date_trunc('day'" in sql
    result = compute(ctx, config_dir=extra_config)
    row = result["rows"][0]
    assert row["current_value"] == 4.0
    assert row["value_3p"] == 7.0


def test_gregorian_quarter_rolls_three_months(tmp_path, extra_config):
    """March 2026 gregorian quarter is Q1 starting 2026-01-01 (Jan+Feb+Mar)."""
    path = tmp_path / "q.parquet"
    rows = []
    for month, amount in (("2026-01-01", 10), ("2026-02-01", 20), ("2026-03-01", 30), ("2025-12-01", 99)):
        rows.append(
            {
                "event_month": month,
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": amount,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(extra_config / "kpis" / "9033.yaml", _grain_kpi(9033, grain="quarter"))
    ctx = make_context(
        path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9033,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] == "2026-01-01"
    assert result["rows"][0]["current_value"] == 60.0


def test_fiscal_quarter_differs_from_gregorian(tmp_path, extra_config):
    """fiscal_start_month=5: March 2026 buckets to 2026-02-01, not gregorian 2026-01-01."""
    path = tmp_path / "fq.parquet"
    pd.DataFrame(
        [
            {"event_month": "2026-02-01", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 5},
            {"event_month": "2026-03-01", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 7},
            {"event_month": "2026-01-01", "region": "NA", "reason_code": "LATE_SUPPLIER", "supplier_name": "ABC", "amount": 100},
        ]
    ).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9034.yaml",
        _grain_kpi(9034, grain="quarter", calendar="fiscal", fiscal_start_month=5),
    )
    ctx = make_context(
        path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9034,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] == "2026-02-01"
    assert result["rows"][0]["current_value"] == 12.0
    assert "INTERVAL" in result["sql"]


def _grain_kpi(
    kpi_id: int,
    *,
    grain: str,
    calendar: str = "gregorian",
    fiscal_start_month: int = 4,
) -> dict:
    """Single global-cut sum KPI at the requested time grain."""
    trailing_key = "days" if grain == "day" else "months"
    time = {
        "column": "event_month",
        "grain": grain,
        "filter_code": "reporting_month",
        "calendar": calendar,
        "timezone": "UTC",
    }
    if calendar == "fiscal":
        time["fiscal_start_month"] = fiscal_start_month
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": time,
        "dimensions": [{"name": "reason_code", "kind": "dimension"}],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "value_3p": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {trailing_key: 3},
                "inclusive": True,
            },
        },
    }
