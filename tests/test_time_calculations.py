"""Time-calculation tests: windows, offsets, exclusive bounds, YoY, lookback.

What this file provides
    Numeric assertions for 3/6/12m windows, calendar point offsets, exclusive
    trailing windows, YoY/MoM, and lookback formulas including arithmetic.

Where it is used
    pytest tests/test_time_calculations.py (DuckDB + Pandas on local parquet).

When to use
    Add a case when a new time op or inclusive flag is added to YAML.
"""

from datetime import date

from kpi_engine import compute, validate
from kpi_engine.core.binder import load_kpi
from kpi_engine.core.time_planner import lookback_for, max_lookback_months
from kpi_engine.dates import add_months, iso_month, month_range_inclusive, parse_month
from kpi_engine.exceptions import TimePlanError
from tests.conftest import find_row, make_context, write_yaml


def test_parse_month_formats():
    """YYYY-MM and YYYY-MM-DD both become first-of-month dates."""
    assert parse_month("2026-03") == date(2026, 3, 1)
    assert parse_month("2026-03-15") == date(2026, 3, 1)
    assert parse_month(date(2026, 3, 20)) == date(2026, 3, 1)


def test_add_months_crosses_year_boundary():
    """Calendar month math wraps years; it does not use row offsets."""
    assert add_months(date(2026, 3, 1), -3) == date(2025, 12, 1)
    assert add_months(date(2025, 12, 1), 1) == date(2026, 1, 1)
    assert month_range_inclusive(date(2025, 11, 1), date(2026, 1, 1)) == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]
    assert iso_month(date(2026, 3, 1)) == "2026-03-01"


def test_inclusive_windows_3_6_12(parquet_path, config_dir):
    """Inclusive trailing N months include the anchor month."""
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m", "value_6m", "value_12m"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # Mar 2026: NA 30 + EU 15 = 45
    assert g["current_value"] == 45.0
    # Jan–Mar 2026: (1+2+3)=6 → NA 60 + EU 30 = 90
    assert g["value_3m"] == 90.0
    # Oct 2025–Mar 2026: (10+11+12+1+2+3)=39 → NA 390 + EU 195 = 585
    assert g["value_6m"] == 585.0
    # Apr 2025–Mar 2026: (4..12 + 1+2+3)=78 → NA 780 + EU 390 = 1170
    assert g["value_12m"] == 1170.0

    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert na["current_value"] == 30.0
    assert na["value_3m"] == 60.0
    assert na["value_6m"] == 390.0
    assert na["value_12m"] == 780.0


def test_exclusive_window_skips_anchor_month(parquet_path, extra_config):
    """inclusive: false uses the N months strictly before the selected month."""
    write_yaml(
        extra_config / "kpis" / "9001.yaml",
        _time_kpi(9001),
    )
    ctx = make_context(
        parquet_path,
        measures=["value_3m_exclusive", "previous_3m", "previous_month"],
        supplier=["ABC"],
        kpi_id=9001,
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # Exclusive 3m: Dec 2025 + Jan + Feb 2026 = (12+1+2)=15 → NA 150 + EU 75 = 225
    assert g["value_3m_exclusive"] == 225.0
    # Point offset 3 months → Dec 2025: NA 120 + EU 60 = 180
    assert g["previous_3m"] == 180.0
    # Point offset 1 month → Feb 2026: NA 20 + EU 10 = 30
    assert g["previous_month"] == 30.0

    plan = validate(ctx, config_dir=extra_config)
    assert plan["lookback_months"] == 3
    assert plan["span_start"] == "2025-12-01"


def test_offset_years_plus_months(parquet_path, extra_config):
    """years and months on a point offset add together as calendar months."""
    write_yaml(extra_config / "kpis" / "9001.yaml", _time_kpi(9001))
    ctx = make_context(
        parquet_path,
        measures=["previous_15m"],
        supplier=["ABC"],
        kpi_id=9001,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # 1 year + 3 months back from 2026-03 → 2024-12, outside the fixture → null
    assert g["previous_15m"] is None
    plan = validate(ctx, config_dir=extra_config)
    assert plan["lookback_months"] == 15
    assert plan["span_start"] == "2024-12-01"


def test_yoy_and_mom_growth(parquet_path, extra_config):
    """growth_pct is (current - prior) / prior; missing prior is null, not a row shift."""
    write_yaml(extra_config / "kpis" / "9001.yaml", _time_kpi(9001))
    ctx = make_context(
        parquet_path,
        measures=["yoy_month", "mom_pct", "current_value", "previous_year_value"],
        supplier=["ABC"],
        kpi_id=9001,
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # current 45, prior year (EU only, NA missing) 15 → (45-15)/15 = 2
    assert g["yoy_month"] == 2.0
    # current 45, Feb 30 → (45-30)/30 = 0.5
    assert g["mom_pct"] == 0.5

    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert na["previous_year_value"] is None
    assert na["yoy_month"] is None


def test_arithmetic_lookback_follows_operands(config_dir):
    """YoY lookback is the max of current (0) and previous_year (12), not a fixed 12 in YAML."""
    kpi = load_kpi(3004, config_dir)
    by_key = {o.key: o for o in kpi.measures}
    assert lookback_for(by_key["yoy_month"], by_key) == 12
    assert lookback_for(by_key["value_6m"], by_key) == 5
    assert lookback_for(by_key["value_12m"], by_key) == 11
    assert max_lookback_months(kpi, ("yoy_month",)) == 12
    assert max_lookback_months(kpi, ("value_6m",)) == 5


def test_exclusive_window_lookback_is_n_not_n_minus_1(extra_config):
    """Exclusive trailing 3 needs 3 months before the anchor (Dec–Feb for a March anchor)."""
    write_yaml(extra_config / "kpis" / "9001.yaml", _time_kpi(9001))
    kpi = load_kpi(9001, extra_config)
    by_key = {o.key: o for o in kpi.measures}
    assert lookback_for(by_key["value_3m_exclusive"], by_key) == 3
    assert lookback_for(by_key["value_3m"], by_key) == 2


def test_multiple_month_values_are_an_error(parquet_path, config_dir):
    """The selected month must be a single value; it is the anchor, not an IN list."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["filters"]["reporting_month"] = {"value": ["2026-03", "2026-02"], "input_text": "simple"}
    try:
        validate(ctx, config_dir=config_dir)
    except TimePlanError as exc:
        assert "exactly one value" in str(exc)
    else:
        raise AssertionError("expected TimePlanError")


def test_business_date_is_not_the_anchor(parquet_path, config_dir):
    """context.business_date is ignored; reporting_month is the only clock."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        month="2026-03",
        business_date="2099-01-01",
    )
    result = compute(ctx, config_dir=config_dir)
    assert result["parameters"]["anchor"] == "2026-03-01"
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0


def _time_kpi(kpi_id: int) -> dict:
    """3004 plus extra time ops used only in these tests."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [
            {
                "name": "G",
                "group_by": ["reason_code"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["reason_code", "region"], "ignore_filters": []},
        ],
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "reason_code": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
            "previous_month": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 1},
            },
            "previous_3m": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 3},
            },
            "previous_15m": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1, "months": 3},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "value_3m_exclusive": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": False,
            },
            "yoy_month": {
                "op": "arithmetic",
                "fn": "growth_pct",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "mom_pct": {
                "op": "arithmetic",
                "fn": "growth_pct",
                "left": "current_value",
                "right": "previous_month",
            },
        },
    }
