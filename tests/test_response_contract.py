"""The JSON contract the platform consumes, and request-level behaviour.

What this file provides
    Response envelope shape, filter metadata, row ordering, pagination modes,
    JSON serializability, determinism, and validate-versus-compute parity.

Where it is used
    pytest tests/test_response_contract.py.

When to use
    Add a case whenever the response gains a field or a caller depends on a
    guarantee (ordering, null dimensions, page metadata).
"""

import json

import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import KPIEngineError
from tests.conftest import make_context, minimal_kpi, write_yaml
from udfs.sotif.main import main


def test_response_envelope_has_every_documented_key(parquet_path, config_dir):
    """Callers read these keys by name; none of them may quietly disappear."""
    ctx = make_context(
        parquet_path, measures=["current_value", "trend_12m"], supplier=["ABC"]
    )
    result = compute(ctx, config_dir=config_dir)
    assert set(result) == {
        "kpi_id",
        "request_id",
        "parameters",
        "request_parameters",
        "applied_filters",
        "ignored_filters",
        "skipped_filters",
        "trend_axes",
        "trend_labels",
        "meta",
        "pagination",
        "sql",
        "sqls",
        "rows",
        "selected_dimensions",
        "applied_cuts",
        "dropped_cuts",
        "grain_warnings",
    }
    assert result["kpi_id"] == 3004
    assert result["request_id"] == "REQ-page-001"
    assert result["parameters"] == {
        "anchor": "2026-03-01",
        "time_grain": "month",
        "span_start": "2025-04-01",
        "lookback_months": 11,
    }
    assert result["request_parameters"] == {}
    assert set(result["pagination"]) == {"page", "page_size", "total_count", "has_more"}


def test_applied_filters_record_where_each_filter_ran(parquet_path, config_dir):
    """Supplier is pushed to DuckDB; region is deferred because cut G ignores it."""
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], region=["NA"]
    )
    result = compute(ctx, config_dir=config_dir)
    by_code = {f["filter_code"]: f for f in result["applied_filters"]}
    assert by_code["Supplier Name"]["stage"] == "extract"
    assert by_code["Supplier Name"]["apply"] == "extract"
    assert by_code["Supplier Name"]["column"] == "supplier_name"
    assert by_code["Supplier Name"]["values"] == ["ABC"]
    assert by_code["region"]["stage"] == "calc"
    assert by_code["region"]["apply"] == "calc"
    assert all(f["op"] == "in" for f in result["applied_filters"])
    assert result["ignored_filters"] == [
        {"filter_code": "region", "reason": "cut_G_ignore_filters"}
    ]


def test_the_time_filter_is_never_reported_as_an_in_filter(parquet_path, config_dir):
    """reporting_month becomes the anchor, so it is not an applied IN filter."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    assert all(f["filter_code"] != "reporting_month" for f in result["applied_filters"])
    assert result["parameters"]["anchor"] == "2026-03-01"


def test_rows_carry_every_dimension_with_null_for_ungrouped_ones(parquet_path, config_dir):
    """A stable row shape lets the caller build one table across cuts."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    rows = compute(ctx, config_dir=config_dir)["rows"]
    for row in rows:
        assert set(row) == {
            "output_cut",
            "grouped_dimensions",
            "reason_code",
            "region",
            "supplier",
            "current_value",
            "model",
        }
        assert row["model"] == "sotif"
    g_rows = [r for r in rows if r["output_cut"] == "G"]
    assert all(r["region"] is None for r in g_rows)
    assert all(r["grouped_dimensions"] == ["reason_code"] for r in g_rows)
    r_rows = [r for r in rows if r["output_cut"] == "R"]
    assert all(r["region"] is not None for r in r_rows)
    assert all(r["grouped_dimensions"] == ["reason_code", "region"] for r in r_rows)


def test_rows_are_sorted_by_cut_then_dimension_order(parquet_path, config_dir):
    """Row order is deterministic so paging is stable across requests."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    rows = compute(ctx, config_dir=config_dir)["rows"]
    keys = [
        (r["output_cut"], r["reason_code"] or "", r["region"] or "") for r in rows
    ]
    assert keys == sorted(keys)
    assert keys[0][0] == "G"


def test_only_requested_measures_are_returned(parquet_path, config_dir):
    """The projection list controls the payload; unrequested measures cost nothing."""
    ctx = make_context(parquet_path, measures=["value_3m"], supplier=["ABC"])
    rows = compute(ctx, config_dir=config_dir)["rows"]
    assert "value_3m" in rows[0]
    assert "current_value" not in rows[0]
    assert "yoy_month" not in rows[0]


def test_empty_projection_does_not_run_the_catalog(parquet_path, config_dir):
    """An empty measures_required list does not expand to every KPI YAML key."""
    ctx = make_context(parquet_path, measures=[], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    assert result["parameters"]["lookback_months"] == 0
    for row in result["rows"]:
        for key in ("current_value", "previous_year_value", "value_3m", "yoy_month", "trend_12m"):
            assert key not in row


def test_response_is_json_serializable_without_nan(parquet_path, config_dir):
    """NaN and numpy scalars are not valid JSON; the engine must emit neither."""
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value", "yoy_month", "trend_12m"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    encoded = json.dumps(result, allow_nan=False)
    assert "NaN" not in encoded
    for row in result["rows"]:
        for value in row.values():
            assert type(value) in (str, float, int, list, type(None)), value


def test_compute_is_deterministic_and_leaves_the_context_untouched(parquet_path, config_dir):
    """Two identical requests return identical payloads; the context is read-only input."""
    ctx = make_context(parquet_path, measures=["current_value", "value_3m"], supplier=["ABC"])
    snapshot = json.dumps(ctx, sort_keys=True)
    first = compute(ctx, config_dir=config_dir)
    second = compute(ctx, config_dir=config_dir)
    assert first == second
    assert json.dumps(ctx, sort_keys=True) == snapshot


def test_udf_entry_point_returns_the_whole_response(parquet_path, config_dir):
    """udfs.sotif.main is a pass-through, not a reduced view of the response."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    assert main(ctx, config_dir=str(config_dir)) == compute(ctx, config_dir=config_dir)


def test_validate_matches_compute_sql_without_reading_data(parquet_path, config_dir):
    """A dry run compiles the same SQL even when the path cannot be scanned."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    assert validate(ctx, config_dir=config_dir)["sql"] == compute(ctx, config_dir=config_dir)["sql"]

    missing = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    missing["datasets"]["Sotif"]["path"] = "/no/such/file.parquet"
    planned = validate(missing, config_dir=config_dir)
    assert planned["ok"] is True
    with pytest.raises(KPIEngineError, match="DuckDB extract failed"):
        compute(missing, config_dir=config_dir)


def test_pagination_without_page_size_returns_everything(parquet_path, config_dir):
    """No page_size means one page of all rows, and page echoes back untouched."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    paging = result["pagination"]
    assert paging["page_size"] is None
    assert paging["page"] is None
    assert paging["has_more"] is False
    assert paging["total_count"] == len(result["rows"]) == 5


def test_limit_is_used_when_page_size_is_absent(parquet_path, config_dir):
    """output.limit behaves as a page size for callers that send only a limit."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], limit=2)
    result = compute(ctx, config_dir=config_dir)
    assert len(result["rows"]) == 2
    assert result["pagination"]["page"] == 1
    assert result["pagination"]["page_size"] == 2
    assert result["pagination"]["has_more"] is True


def test_page_past_the_end_is_empty_but_reports_the_total(parquet_path, config_dir):
    """Overshooting the last page returns no rows without failing the request."""
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], page=9, page_size=2
    )
    result = compute(ctx, config_dir=config_dir)
    assert result["rows"] == []
    assert result["pagination"]["total_count"] == 5
    assert result["pagination"]["has_more"] is False


def test_pages_partition_the_full_result_without_overlap(parquet_path, config_dir):
    """Walking every page reproduces the unpaged result exactly once."""
    full = compute(
        make_context(parquet_path, measures=["current_value"], supplier=["ABC"]),
        config_dir=config_dir,
    )["rows"]
    collected = []
    for page in (1, 2, 3):
        ctx = make_context(
            parquet_path,
            measures=["current_value"],
            supplier=["ABC"],
            page=page,
            page_size=2,
        )
        collected.extend(compute(ctx, config_dir=config_dir)["rows"])
    assert collected == full


def test_multi_model_requests_report_one_sql_per_extract(parquet_path, extra_config):
    """sqls exposes every query the request ran; sql stays the first for compatibility."""
    write_yaml(
        extra_config / "kpis" / "9700.yaml",
        minimal_kpi(
            9700,
            base_measures={
                "sotif_value": {"sql": "amount", "agg": "sum"},
                "distinct_suppliers": {"sql": "supplier_name", "agg": "count_distinct"},
            },
            measures={
                "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
                "supplier_count": {
                    "of": "distinct_suppliers",
                    "op": "point",
                    "offset": {"months": 0},
                },
            },
        ),
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value", "supplier_count"],
        supplier=["ABC"],
        kpi_id=9700,
    )
    result = compute(ctx, config_dir=extra_config)
    assert len(result["sqls"]) == 1
    assert result["sql"] == result["sqls"][0]
    row = next(r for r in result["rows"] if r["output_cut"] == "G")
    assert row["supplier_count"] == 1.0


def test_trend_axes_only_lists_requested_trends(parquet_path, config_dir):
    """Trend metadata mirrors the projection, so callers can zip axis to values."""
    without = compute(
        make_context(parquet_path, measures=["current_value"], supplier=["ABC"]),
        config_dir=config_dir,
    )
    assert without["trend_axes"] == {}

    with_trend = compute(
        make_context(parquet_path, measures=["trend_12m"], supplier=["ABC"]),
        config_dir=config_dir,
    )
    assert list(with_trend["trend_axes"]) == ["trend_12m"]
    assert list(with_trend["trend_labels"]) == ["trend_12m"]
    assert len(with_trend["trend_labels"]["trend_12m"]) == len(
        with_trend["trend_axes"]["trend_12m"]
    )
    assert without["trend_labels"] == {}
    assert without["meta"] is None
