"""Dimension roles on JSON rows and per-measure period labels.

What this file provides
    Grain values vs rollup null vs omitted catalog dims. Point/window/trend
    cells carry their period next to the value. Cut-phase and YoY stay scalar.

Where it is used
    pytest tests/test_output_shape.py.

When to use
    Add a case when the response row shape or period wrapping changes.
"""

from kpi_engine import compute
from tests.conftest import find_row, make_context, value_of


def test_g_and_r_share_region_key_and_omit_supplier(parquet_path, config_dir):
    """Region is null on G (worldwide) and set on R; supplier is not in the grain."""
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value", "trend_12m"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["reason_code"] == "LATE_SUPPLIER"
    assert g["region"] is None
    assert "supplier" not in g
    assert "region" not in g["grouped_dimensions"]

    r = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert r["region"] == "NA"
    assert "supplier" not in r
    assert r["grouped_dimensions"] == ["reason_code", "region"]


def test_point_and_lag_carry_the_effective_period(parquet_path, config_dir):
    """previous_year_value is March 2025, not the request month."""
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value"],
        supplier=["ABC"],
    )
    g = find_row(compute(ctx, config_dir=config_dir), cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == {"value": 45.0, "period": "2026-03-01"}
    assert g["previous_year_value"] == {"value": 15.0, "period": "2025-03-01"}


def test_window_carries_inclusive_bounds(parquet_path, config_dir):
    ctx = make_context(parquet_path, measures=["value_3m"], supplier=["ABC"])
    g = find_row(compute(ctx, config_dir=config_dir), cut="G", reason="LATE_SUPPLIER")
    assert g["value_3m"]["period_start"] == "2026-01-01"
    assert g["value_3m"]["period_end"] == "2026-03-01"
    assert g["value_3m"]["value"] == 90.0


def test_trend_pairs_period_with_each_value(parquet_path, config_dir):
    ctx = make_context(parquet_path, measures=["trend_12m"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    points = g["trend_12m"]
    assert isinstance(points, list) and points
    assert all("period" in item and "value" in item for item in points)
    axis = result["trend_axes"]["trend_12m"]
    assert [item["period"] for item in points] == axis
    assert len(points) == len(axis)
    assert points[-1]["period"] == "2026-03-01"
    assert points[-1]["value"] == 45.0


def test_percent_and_yoy_stay_scalars(parquet_path, config_dir):
    ctx = make_context(
        parquet_path,
        measures=["current_value", "yoy_month"],
        supplier=["ABC"],
    )
    g = find_row(compute(ctx, config_dir=config_dir), cut="G", reason="LATE_SUPPLIER")
    assert isinstance(g["current_value"], dict)
    assert isinstance(g["yoy_month"], (float, type(None)))


def test_value_of_unwraps_wrapped_cells(parquet_path, config_dir):
    ctx = make_context(
        parquet_path,
        measures=["current_value", "trend_12m"],
        supplier=["ABC"],
    )
    g = find_row(compute(ctx, config_dir=config_dir), cut="G", reason="LATE_SUPPLIER")
    assert value_of(g, "current_value") == 45.0
    assert value_of(g, "trend_12m")[-1] == 45.0
