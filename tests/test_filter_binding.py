"""Filter binding: how a context filter_code becomes a WHERE clause or a cut mask.

What this file provides
    Resolution order (context mappings, YAML filter_map, direct column match),
    case and spacing tolerance, deferral to cuts, and the empty-IN contract.

Where it is used
    pytest tests/test_filter_binding.py.

When to use
    Add a case when filter resolution or the source/cut split changes.
"""

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.contracts import BoundFilter, CutSpec
from kpi_engine.core.filters import apply_cut_filters, split_for_duckdb
from kpi_engine.exceptions import FilterError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_filter_named_like_a_column_binds_without_any_mapping(parquet_path, extra_config):
    """A context filter whose code already matches a source column needs no mapping."""
    write_yaml(extra_config / "kpis" / "9800.yaml", minimal_kpi(9800))
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9800,
        extra_filters={"reason_code": {"values": ["LATE_SUPPLIER"], "input_text": "simple"}},
    )
    ctx["datasets"]["Sotif"]["filter_column_mappings"] = [
        {"filter_code": "Supplier Name", "column_name": "supplier_name", "operator": "in"}
    ]
    planned = validate(ctx, config_dir=extra_config)
    assert '"reason_code" IN (?)' in planned["sql"]

    result = compute(ctx, config_dir=extra_config)
    assert {row["reason_code"] for row in result["rows"]} == {"LATE_SUPPLIER"}


def test_region_and_reason_code_spellings_are_the_same(parquet_path, extra_config):
    """Region / region and Reason_code / reason_code bind and ignore the same way."""
    write_yaml(extra_config / "kpis" / "9820.yaml", minimal_kpi(9820))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9820)
    ctx["filters"]["Region"] = {"values": ["NA"], "input_text": "simple"}
    ctx["filters"]["Reason_Code"] = {"values": ["LATE_SUPPLIER"], "input_text": "simple"}
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert '"reason_code" IN (?)' in sql
    assert '"region" IN' not in sql
    result = compute(ctx, config_dir=extra_config)
    assert {row["reason_code"] for row in result["rows"]} == {"LATE_SUPPLIER"}
    r_regions = {row["region"] for row in result["rows"] if row["output_cut"] == "R"}
    assert r_regions == {"NA"}


def test_filter_codes_are_matched_ignoring_case_and_spaces(parquet_path, extra_config):
    """`Supplier Name`, `supplier name`, and `supplier_name` are the same filter."""
    write_yaml(extra_config / "kpis" / "9801.yaml", minimal_kpi(9801))
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9801)
    ctx["filters"]["SUPPLIER name"] = {"value": ["ABC"], "input_text": "simple"}
    planned = validate(ctx, config_dir=extra_config)
    assert '"supplier_name" IN (?)' in planned["sql"]


def test_context_mapping_wins_over_a_same_named_column(parquet_path, extra_config):
    """An explicit mapping redirects a filter even if a column shares its name."""
    write_yaml(extra_config / "kpis" / "9802.yaml", minimal_kpi(9802))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9802)
    ctx["datasets"]["Sotif"]["filter_column_mappings"].append(
        {"filter_code": "reason_code", "column_name": "region", "operator": "in"}
    )
    ctx["filters"]["reason_code"] = {"values": ["NA"], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    assert {row["region"] for row in result["rows"] if row["output_cut"] == "R"} == {"NA"}


def test_yaml_filter_map_overrides_a_context_mapping(parquet_path, extra_config):
    """KPI YAML is the last word on where a filter lands for this KPI."""
    write_yaml(
        extra_config / "kpis" / "9803.yaml",
        minimal_kpi(9803, filter_map={"Supplier Name": "reason_code"}),
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9803)
    ctx["filters"]["Supplier Name"] = {"values": ["LATE_SUPPLIER"], "input_text": "simple"}
    planned = validate(ctx, config_dir=extra_config)
    assert '"reason_code" IN (?)' in planned["sql"]
    assert '"supplier_name" IN' not in planned["sql"]


def test_filter_mapped_to_a_column_that_does_not_exist_is_rejected(parquet_path, extra_config):
    """A mapping onto a missing column fails at bind, not with a DuckDB error."""
    write_yaml(
        extra_config / "kpis" / "9804.yaml",
        minimal_kpi(9804, filter_map={"plant_code": "plant_code"}),
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9804,
        extra_filters={"plant_code": {"values": ["P1"], "input_text": "simple"}},
    )
    ctx["datasets"]["Sotif"]["columns"] = [
        c for c in ctx["datasets"]["Sotif"]["columns"] if c != "plant_code"
    ]
    with pytest.raises(FilterError, match="does not bind to a source column"):
        validate(ctx, config_dir=extra_config)


def test_filters_ignored_by_any_emitted_cut_stay_out_of_duckdb(parquet_path, config_dir):
    """G ignores region, so region cannot be pushed down or the global cut would shrink."""
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], region=["NA"]
    )
    planned = validate(ctx, config_dir=config_dir)
    assert '"region" IN' not in planned["sql"]
    assert '"supplier_name" IN' in planned["sql"]


def test_deferred_filters_apply_only_to_cuts_that_do_not_ignore_them(parquet_path, config_dir):
    """The same request yields a worldwide G row and NA-only R rows."""
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], region=["NA"]
    )
    result = compute(ctx, config_dir=config_dir)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert g["current_value"] == 45.0
    assert na["current_value"] == 30.0
    assert {r["region"] for r in result["rows"] if r["output_cut"] == "R"} == {"NA"}


def test_split_for_duckdb_matches_ignore_names_by_code_or_column():
    """ignore_filters may name the filter code or the physical column."""
    cuts = (
        CutSpec(name="G", group_by=(), ignore_filters=("Region",), also_emit=()),
    )
    by_code = BoundFilter(code="Region", column="region_name", values=("NA",), stage="extract")
    by_column = BoundFilter(code="plant", column="Region", values=("NA",), stage="extract")
    other = BoundFilter(code="supplier", column="supplier_name", values=("ABC",), stage="extract")
    source, deferred = split_for_duckdb((by_code, by_column, other), cuts)
    assert [f.code for f in deferred] == ["Region", "plant"]
    assert [f.code for f in source] == ["supplier"]


def test_apply_cut_filters_skips_columns_the_frame_does_not_have():
    """A deferred filter on a column outside this cut's frame is a no-op, not a crash."""
    frame = pd.DataFrame([{"region": "NA", "value": 1.0}])
    cut = CutSpec(name="R", group_by=("region",), ignore_filters=(), also_emit=())
    absent = BoundFilter(code="plant", column="plant_code", values=("P1",), stage="calc")
    assert len(apply_cut_filters(frame, cut, (absent,))) == 1


def test_apply_cut_filters_with_no_values_matches_nothing():
    """An empty IN list is an explicit "nothing selected", not "everything"."""
    frame = pd.DataFrame([{"region": "NA", "value": 1.0}])
    cut = CutSpec(name="R", group_by=("region",), ignore_filters=(), also_emit=())
    empty = BoundFilter(code="region", column="region", values=(), stage="calc")
    assert apply_cut_filters(frame, cut, (empty,)).empty


def test_empty_region_skips_and_emits_all_r_rows(parquet_path, config_dir):
    """region=[] skips the predicate; G is worldwide and R lists every region."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], region=[])
    result = compute(ctx, config_dir=config_dir)
    assert {"filter_code": "region", "reason": "blank"} in result["skipped_filters"]
    cuts = {row["output_cut"] for row in result["rows"]}
    assert cuts == {"G", "R"}
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA", "EU"}
