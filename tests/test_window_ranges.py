"""Named window ranges: mtd, qtd, ytd, wtd, full period, and window offset.

What this file provides
    Bind grammar, February QTD vs trailing 3, prior-year QTD, fiscal QTD,
    wtd day-grain, full_quarter lookforward without zero-fill.

Where it is used
    pytest tests/test_window_ranges.py.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.contracts import OutputSpec, TimeSpec
from kpi_engine.core.binder import load_kpi
from kpi_engine.core.time_planner import lookback_for, lookforward_for
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_february_qtd_is_not_trailing_3(parquet_path, extra_config):
    """Feb QTD is Jan–Feb; trailing 3 is Dec–Feb."""
    spec = minimal_kpi(
        9701,
        measures={
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "value_qtd": {"of": "sotif_value", "op": "window", "range": "qtd"},
        },
    )
    write_yaml(extra_config / "kpis" / "9701.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_3m", "value_qtd"],
        supplier=["ABC"],
        kpi_id=9701,
        month="2026-02",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    # NA LATE base 10: QTD 10+20=30. Trailing 3: Dec 120 + Jan 10 + Feb 20 = 150.
    assert row["value_qtd"] == 30.0
    assert row["value_3m"] == 150.0
    assert row["value_qtd"] != row["value_3m"]


def test_march_qtd_matches_trailing_3(parquet_path, extra_config):
    """March is the last month of Q1, so QTD equals inclusive trailing 3."""
    spec = minimal_kpi(
        9702,
        measures={
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "value_qtd": {"of": "sotif_value", "op": "window", "range": "qtd"},
        },
    )
    write_yaml(extra_config / "kpis" / "9702.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_3m", "value_qtd"],
        supplier=["ABC"],
        kpi_id=9702,
        month="2026-03",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["value_qtd"] == row["value_3m"] == 60.0


def test_may_qtd_is_april_and_may(parquet_path, extra_config):
    spec = minimal_kpi(
        9703,
        measures={"value_qtd": {"of": "sotif_value", "op": "window", "range": "qtd"}},
    )
    write_yaml(extra_config / "kpis" / "9703.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_qtd"],
        supplier=["ABC"],
        kpi_id=9703,
        month="2025-05",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["value_qtd"] == 90.0  # 40 + 50


def test_qtd_offset_is_prior_year_quarter(parquet_path, extra_config):
    """offset years 1 then QTD is Jan–Mar 2025, not 2026."""
    spec = minimal_kpi(
        9704,
        measures={
            "qtd_ly": {
                "of": "sotif_value",
                "op": "window",
                "range": "qtd",
                "offset": {"years": 1},
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9704.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["qtd_ly"],
        supplier=["ABC"],
        kpi_id=9704,
        month="2026-03",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    # 2025 Jan 10 + Feb 20; March NA LATE is missing → 30.
    assert row["qtd_ly"] == 30.0


def test_ytd_alias_matches_cumulative(parquet_path, extra_config):
    spec = minimal_kpi(
        9705,
        measures={
            "a": {"of": "sotif_value", "op": "window", "range": "ytd"},
            "b": {"of": "sotif_value", "op": "window", "range": "cumulative"},
        },
    )
    write_yaml(extra_config / "kpis" / "9705.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["a", "b"],
        supplier=["ABC"],
        kpi_id=9705,
        month="2026-03",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["a"] == row["b"]


def test_fiscal_qtd_starts_in_april(parquet_path, extra_config):
    spec = minimal_kpi(
        9706,
        measures={"value_qtd": {"of": "sotif_value", "op": "window", "range": "qtd"}},
    )
    spec["time"]["calendar"] = "fiscal"
    spec["time"]["fiscal_start_month"] = 4
    write_yaml(extra_config / "kpis" / "9706.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_qtd"],
        supplier=["ABC"],
        kpi_id=9706,
        month="2025-05",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["value_qtd"] == 90.0  # Apr+May of fiscal Q1


def test_qtd_rejects_trailing(extra_config):
    spec = minimal_kpi(
        9707,
        measures={
            "bad": {
                "of": "sotif_value",
                "op": "window",
                "range": "qtd",
                "trailing": {"months": 3},
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9707.yaml", spec)
    with pytest.raises(BindError, match="cannot set trailing"):
        load_kpi(9707, extra_config)


def test_wtd_rejects_month_grain(extra_config):
    spec = minimal_kpi(
        9708,
        measures={"w": {"of": "sotif_value", "op": "window", "range": "wtd"}},
    )
    write_yaml(extra_config / "kpis" / "9708.yaml", spec)
    with pytest.raises(BindError, match="time.grain: day"):
        load_kpi(9708, extra_config)


def test_wtd_fails_on_month_pick_when_day_is_allowed(parquet_path, extra_config):
    """wtd binds when day is in time.grains, then fails if the pick is not day."""
    spec = minimal_kpi(
        9711,
        time={
            "column": "event_month",
            "grain": "month",
            "source_grain": "day",
            "grains": ["day", "week", "month"],
            "filter_code": "reporting_month",
        },
        parameters={"time_grain": {"type": "string"}},
        measures={"w": {"of": "sotif_value", "op": "window", "range": "wtd"}},
    )
    write_yaml(extra_config / "kpis" / "9711.yaml", spec)
    load_kpi(9711, extra_config)
    ctx = make_context(
        parquet_path, measures=["w"], supplier=["ABC"], kpi_id=9711, time_grain="month"
    )
    with pytest.raises(BindError, match="time.grain: day"):
        compute(ctx, config_dir=extra_config)


def test_wtd_on_day_grain(tmp_path, extra_config):
    """Wednesday WTD is Mon–Wed."""
    frame = pd.DataFrame(
        [
            {
                "event_month": f"2026-03-{day:02d}",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": 1,
            }
            for day in range(9, 14)  # Mon 9 – Fri 13
        ]
    )
    path = tmp_path / "days.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9709,
        measures={"value_wtd": {"of": "sotif_value", "op": "window", "range": "wtd"}},
    )
    spec["time"]["grain"] = "day"
    write_yaml(extra_config / "kpis" / "9709.yaml", spec)
    ctx = make_context(
        path,
        measures=["value_wtd"],
        supplier=["ABC"],
        kpi_id=9709,
        month="2026-03-11",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["value_wtd"] == 3.0


def test_full_quarter_lookforward_does_not_zero_fill_missing_month(tmp_path, extra_config):
    """Feb full_quarter looks at March; no March rows stay null, not 0."""
    frame = pd.DataFrame(
        [
            {
                "event_month": month,
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": value,
            }
            for month, value in (("2026-01-01", 10), ("2026-02-01", 20))
        ]
    )
    path = tmp_path / "partial_q.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9710,
        measures={
            "qtd": {"of": "sotif_value", "op": "window", "range": "qtd"},
            "full_q": {"of": "sotif_value", "op": "window", "range": "full_quarter"},
        },
    )
    write_yaml(extra_config / "kpis" / "9710.yaml", spec)
    ctx = make_context(
        path,
        measures=["qtd", "full_q"],
        supplier=["ABC"],
        kpi_id=9710,
        month="2026-02",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["qtd"] == 30.0
    assert row["full_q"] == 30.0


def test_full_quarter_lookforward_periods():
    monthly = TimeSpec(column="event_month", grain="month", filter_code="reporting_month")
    spec = OutputSpec(key="fq", kind="window", of="v", window_range="full_quarter")
    assert lookback_for(spec, {}, monthly, anchor=date(2026, 2, 1)) == 1
    assert lookforward_for(spec, {}, time=monthly, anchor=date(2026, 2, 1)) == 1


def test_qtd_lookback_in_february():
    monthly = TimeSpec(column="event_month", grain="month", filter_code="reporting_month")
    spec = OutputSpec(key="qtd", kind="window", of="v", window_range="qtd")
    assert lookback_for(spec, {}, monthly, anchor=date(2026, 2, 1)) == 1
    assert lookforward_for(spec, {}, time=monthly, anchor=date(2026, 2, 1)) == 0
