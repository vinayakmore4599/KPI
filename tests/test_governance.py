"""Phase 3 WS8: measure having, required notes, sort, max_rows, trend page, quality_flags."""

from __future__ import annotations

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def test_measure_having_nulls_without_dropping_row(parquet_path, extra_config):
    spec = minimal_kpi(
        98501,
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "having": [{"of": "current_value", "cmp": "gt", "value": 100000}],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "98501.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98501
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "current_value") is None
    assert result["rows"]


def test_required_measure_null_notes(parquet_path, extra_config):
    spec = minimal_kpi(
        98502,
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 10},
                "required": True,
            }
        },
    )
    write_yaml(extra_config / "kpis" / "98502.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98502
    )
    result = compute(ctx, config_dir=extra_config)
    codes = {n.get("code") for n in result["notes"]}
    assert "required_measure_null" in codes
    assert result["quality_flags"].get("required_measure_null") is True


def test_sort_and_max_rows(parquet_path, extra_config):
    spec = minimal_kpi(
        98503,
        sort=[{"key": "current_value", "order": "desc"}],
        max_rows=1,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98503.yaml", spec)
    kpi = load_kpi(98503, extra_config)
    assert kpi.max_rows == 1
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98503
    )
    result = compute(ctx, config_dir=extra_config)
    assert len(result["rows"]) == 1


def test_quality_flags_empty_result(parquet_path, extra_config):
    spec = minimal_kpi(
        98504,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98504.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["NOPE"],
        kpi_id=98504,
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["rows"] == []
    assert result["quality_flags"].get("empty_result") is True


def test_trend_pagination_slices_measure_keeps_axes(parquet_path, extra_config):
    spec = minimal_kpi(
        98505,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "trend_12m": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 6},
                "cuts": ["G"],
            },
        },
    )
    write_yaml(extra_config / "kpis" / "98505.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["trend_12m"],
        supplier=["ABC"],
        kpi_id=98505,
    )
    ctx["output"] = {"trend_page": 1, "trend_page_size": 2}
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cell = row["trend_12m"]
    assert isinstance(cell, list)
    assert len(cell) <= 2
    axis = result["trend_axes"]["trend_12m"]
    assert len(axis) == 6
    assert result["pagination"].get("trend_page_size") == 2
