"""Host year part forces calendar Jan–Dec years; fiscal quarters stay fiscal.

What this file provides
    Characterize the March–April fiscal year-part bug, then lock year_basis=
    calendar so ytd / full_year / year grain / year-part spans are Jan–Dec
    when the host sends a year part. Fiscal QTD without a year part is unchanged.

Where it is used
    pytest tests/test_year_basis.py.

When to use
    Add a case when year_basis, fiscal year-part selection, or ytd-with-year
    changes.
"""

from datetime import date

from kpi_engine import compute, validate
from kpi_engine.contracts import TimeSpec
from kpi_engine.dates import truncate_period, uses_calendar_year, year_start
from kpi_engine.pipeline.period_select import selection_bounds, selection_periods
from tests.conftest import find_row, make_context, minimal_kpi, value_of, write_yaml


def _fiscal_periods_kpi(kpi_id: int, **overrides):
    spec = minimal_kpi(
        kpi_id,
        time={
            "column": "event_month",
            "grain": "month",
            "calendar": "fiscal",
            "fiscal_start_month": 4,
            "periods": {"year": "year", "month": "current month"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "ytd_value": {"of": "sotif_value", "op": "window", "range": "ytd"},
            "full_year": {"of": "sotif_value", "op": "window", "range": "full_year"},
        },
    )
    spec.update(overrides)
    return spec


def test_year_basis_calendar_truncates_fiscal_kpi_to_january():
    """year_basis=calendar wins over calendar: fiscal for year grain / year_start."""
    time = TimeSpec(
        column="event_month",
        grain="year",
        filter_code="reporting_month",
        calendar="fiscal",
        fiscal_start_month=4,
        year_basis="calendar",
    )
    assert uses_calendar_year(time)
    assert truncate_period(date(2026, 3, 17), time) == date(2026, 1, 1)
    assert year_start(date(2026, 3, 17), time) == date(2026, 1, 1)
    fiscal = TimeSpec(
        column="event_month",
        grain="year",
        filter_code="reporting_month",
        calendar="fiscal",
        fiscal_start_month=4,
    )
    assert not uses_calendar_year(fiscal)
    assert truncate_period(date(2026, 3, 17), fiscal) == date(2025, 4, 1)


def test_year_part_on_fiscal_kpi_selects_january_to_december():
    """Year 2026 on a fiscal KPI is calendar 2026, not April 2026–March 2027."""
    time = TimeSpec(
        column="event_month",
        grain="month",
        filter_code="",
        calendar="fiscal",
        fiscal_start_month=4,
        year_basis="calendar",
    )
    parts = {"year": (2026,)}
    bounds = selection_bounds(parts, time)
    periods = selection_periods(parts, bounds, time)
    assert periods[0] == date(2026, 1, 1)
    assert periods[-1] == date(2026, 12, 1)


def test_year_part_ytd_is_january_to_anchor(parquet_path, extra_config):
    """YTD for year=2026 month=March on a fiscal KPI is Jan–Mar, not Apr–Mar."""
    write_yaml(extra_config / "kpis" / "9801.yaml", _fiscal_periods_kpi(9801))
    ctx = make_context(
        parquet_path,
        measures=["current_value", "ytd_value"],
        supplier=["ABC"],
        kpi_id=9801,
    )
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["year"] = {"value": [2026], "input_text": "simple"}
    ctx["filters"]["current month"] = {"value": [3], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["time_selection"]["start"] == "2026-03-01"
    assert value_of(g, "current_value") == 45.0
    # Jan+Feb+Mar 2026: (1+2+3)=6 → NA 60 + EU 30 = 90. Fiscal Apr–Mar would be 1170.
    assert value_of(g, "ytd_value") == 90.0
    assert g["ytd_value"]["period_start"] == "2026-01-01"
    assert g["ytd_value"]["period_end"] == "2026-03-01"


def test_fiscal_qtd_without_year_part_still_starts_in_april(parquet_path, extra_config):
    """A legacy month filter on a fiscal KPI keeps April fiscal quarters."""
    spec = minimal_kpi(
        9802,
        measures={"value_qtd": {"of": "sotif_value", "op": "window", "range": "qtd"}},
    )
    spec["time"]["calendar"] = "fiscal"
    spec["time"]["fiscal_start_month"] = 4
    write_yaml(extra_config / "kpis" / "9802.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_qtd"],
        supplier=["ABC"],
        kpi_id=9802,
        month="2025-05",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "value_qtd") == 90.0


def test_fiscal_quarter_plus_calendar_year_stays_on_fiscal_quarter(parquet_path, extra_config):
    """Year 2025 + fiscal Q2 is still Jul–Sep; year_basis does not move quarters."""
    spec = _fiscal_periods_kpi(
        9803,
        time={
            "column": "event_month",
            "grain": "month",
            "calendar": "fiscal",
            "fiscal_start_month": 4,
            "periods": {"year": "year", "quarter": "quarter"},
        },
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "9803.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9803
    )
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["year"] = {"value": [2025], "input_text": "simple"}
    ctx["filters"]["quarter"] = {"value": [2], "input_text": "simple"}
    planned = validate(ctx, config_dir=extra_config)
    assert planned["anchor"] == "2025-09-01"
