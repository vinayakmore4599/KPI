"""Request-level scenarios: pagination, filters, SQL models, empty extracts.

What this file provides
    Pagination slices, empty IN skips, unmapped filters fail, kind: sql
    models scan via $alias_path, unknown measure ops fail at bind.

Where it is used
    pytest tests/test_request_scenarios.py.

When to use
    Add a case for a new request envelope or model kind behaviour.
"""

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError, FilterError
from tests.conftest import find_row, make_context, write_yaml


def test_pagination_slices_after_calc(parquet_path, config_dir):
    """page_size slices calculated rows; total_count stays the full size."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        page=1,
        page_size=2,
    )
    result = compute(ctx, config_dir=config_dir)
    assert result["pagination"]["page"] == 1
    assert result["pagination"]["page_size"] == 2
    assert result["pagination"]["total_count"] == 5
    assert result["pagination"]["has_more"] is True
    assert len(result["rows"]) == 2

    ctx["output"]["page"] = 3
    last = compute(ctx, config_dir=config_dir)
    assert last["pagination"]["has_more"] is False
    assert len(last["rows"]) == 1


def test_empty_in_list_skips_the_filter(parquet_path, config_dir):
    """Empty supplier IN is skipped, not FALSE — the extract is unfiltered on supplier."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=[],
    )
    planned = validate(ctx, config_dir=config_dir)
    assert "FALSE" not in planned["sql"]
    assert planned["skipped_filters"] == [
        {"filter_code": "Supplier Name", "reason": "blank"}
    ]
    result = compute(ctx, config_dir=config_dir)
    assert result["rows"]
    assert result["skipped_filters"] == [
        {"filter_code": "Supplier Name", "reason": "blank"}
    ]


def test_unmapped_filter_is_hard_error(parquet_path, config_dir):
    """Filters without a column mapping fail; they are not silently dropped."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        extra_filters={"not_a_column": {"value": ["x"], "input_text": "simple"}},
    )
    try:
        validate(ctx, config_dir=config_dir)
    except FilterError as exc:
        assert "not_a_column" in str(exc)
    else:
        raise AssertionError("expected FilterError")


def test_sql_model_matches_physical(parquet_path, extra_config):
    """kind: sql wrapping read_parquet($alias_path) agrees with the physical model."""
    write_yaml(
        extra_config / "models" / "sotif_sql.yaml",
        {
            "model_id": "sotif_sql",
            "kind": "sql",
            "required_aliases": ["sotif"],
            "sql": "SELECT event_month, region, reason_code, supplier_name, amount\n"
            "FROM read_parquet($sotif_path)",
        },
    )
    spec = {
        "kpi_id": 9011,
        "version": 1,
        "model": "sotif_sql",
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
        "default_dimensions": ["reason_code"],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [
            {
                "name": "G",
                "group_by": [],
                "exclude_from_grain": ["region"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    }
    write_yaml(extra_config / "kpis" / "9011.yaml", spec)
    sql_ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9011,
    )
    physical_ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=3004,
    )
    sql_result = compute(sql_ctx, config_dir=extra_config)
    physical = compute(physical_ctx, config_dir=extra_config)
    assert find_row(sql_result, cut="G", reason="LATE_SUPPLIER")["value_3m"] == 90.0
    assert (
        find_row(sql_result, cut="G", reason="LATE_SUPPLIER")["current_value"]
        == find_row(physical, cut="G", reason="LATE_SUPPLIER")["current_value"]
    )


def test_unknown_op_fails_at_bind(extra_config):
    """YAML cannot invent an op; only the catalog kinds are valid."""
    write_yaml(
        extra_config / "kpis" / "9012.yaml",
        {
            "kpi_id": 9012,
            "version": 1,
            "model": "sotif",
            "time": {
                "column": "event_month",
                "grain": "month",
                "filter_code": "reporting_month",
            },
            "dimensions": [{"name": "reason_code", "kind": "dimension"}],
            "default_dimensions": ["reason_code"],
            "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
            "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
            "default_cut": "G",
            "measures": {
                "sneaky": {"op": "eval", "of": "sotif_value"},
            },
        },
    )
    try:
        load_kpi(9012, extra_config)
    except BindError as exc:
        assert "eval" in str(exc)
    else:
        raise AssertionError("expected BindError")
