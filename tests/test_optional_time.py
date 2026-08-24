"""Time is per KPI YAML: any filter_code, or no time column at all.

What this file provides
    Snapshot KPIs (omit time:) do not require a month filter.
    Period KPIs claim whichever context filter time.filter_code names.

Where it is used
    pytest tests/test_optional_time.py.

When to use
    Add a case when a new KPI has a different period filter or no calendar.
"""

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.core.binder import load_kpi
from kpi_engine.exceptions import BindError, TimePlanError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _snapshot_kpi(kpi_id: int) -> dict:
    """Minimal KPI with no time block and only a current point measure."""
    return minimal_kpi(
        kpi_id,
        time=None,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )


def test_omitted_time_block_is_a_snapshot_kpi(extra_config):
    """No time: in YAML means no period column and no required month filter."""
    write_yaml(extra_config / "kpis" / "9700.yaml", _snapshot_kpi(9700))
    kpi = load_kpi(9700, extra_config)
    assert kpi.time is None


def test_snapshot_kpi_computes_without_a_month_filter(parquet_path, extra_config):
    """All matching rows are aggregated; reporting_month is not required."""
    write_yaml(extra_config / "kpis" / "9701.yaml", _snapshot_kpi(9701))
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9701
    )
    del ctx["filters"]["reporting_month"]
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] is None
    assert result["parameters"]["time_grain"] is None
    assert "date_trunc" not in result["sql"].lower()
    frame = pd.read_parquet(parquet_path)
    expected = float(frame.loc[frame["reason_code"] == "LATE_SUPPLIER", "amount"].sum())
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert row["current_value"] == expected


def test_snapshot_kpi_skips_host_reporting_month(parquet_path, extra_config):
    """Usual host context still has reporting_month; snapshot skips it as no_time."""
    write_yaml(extra_config / "kpis" / "9706.yaml", _snapshot_kpi(9706))
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9706
    )
    result = compute(ctx, config_dir=extra_config)
    assert any(
        item.get("filter_code") == "reporting_month" and item.get("reason") == "no_time"
        for item in result["skipped_filters"]
    )
    assert result["parameters"]["anchor"] is None
    frame = pd.read_parquet(parquet_path)
    expected = float(frame.loc[frame["reason_code"] == "LATE_SUPPLIER", "amount"].sum())
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert row["current_value"] == expected


def test_snapshot_kpi_rejects_window_measures(extra_config):
    """Windows and trends need a time column; snapshot YAML cannot declare them."""
    spec = _snapshot_kpi(9702)
    spec["measures"]["value_3m"] = {
        "of": "sotif_value",
        "op": "window",
        "trailing": {"months": 3},
        "inclusive": True,
    }
    write_yaml(extra_config / "kpis" / "9702.yaml", spec)
    with pytest.raises(BindError, match="no time: block"):
        load_kpi(9702, extra_config)


def test_time_filter_code_is_whatever_the_kpi_declares(parquet_path, extra_config):
    """A KPI that anchors on as_of_period does not look for reporting_month."""
    spec = minimal_kpi(
        9703,
        time={
            "column": "event_month",
            "grain": "month",
            "filter_code": "as_of_period",
            "calendar": "gregorian",
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    write_yaml(extra_config / "kpis" / "9703.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9703
    )
    ctx["filters"]["as_of_period"] = ctx["filters"].pop("reporting_month")
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] == "2026-03-01"
    assert all(f["filter_code"] != "as_of_period" for f in result["applied_filters"])


def test_declared_time_still_requires_its_filter(parquet_path, extra_config):
    """Omitting the named period filter on a time-based KPI is still an error."""
    spec = minimal_kpi(
        9704,
        time={
            "column": "event_month",
            "grain": "month",
            "filter_code": "fiscal_month",
            "calendar": "gregorian",
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    write_yaml(extra_config / "kpis" / "9704.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9704
    )
    with pytest.raises(TimePlanError, match="fiscal_month"):
        compute(ctx, config_dir=extra_config)


def test_time_block_requires_column_and_filter_code(extra_config):
    """A partial time: block is not treated as a snapshot KPI."""
    spec = _snapshot_kpi(9705)
    spec["time"] = {"grain": "month"}
    write_yaml(extra_config / "kpis" / "9705.yaml", spec)
    with pytest.raises(BindError, match="time.column is required"):
        load_kpi(9705, extra_config)

    spec["time"] = {"column": "event_month"}
    write_yaml(extra_config / "kpis" / "9705.yaml", spec)
    with pytest.raises(BindError, match="time.filter_code is required"):
        load_kpi(9705, extra_config)
