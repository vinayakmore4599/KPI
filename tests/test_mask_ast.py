"""Phase 1 mask AST: like/or/not/also_where/regexp, snapshot filtered_point."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, value_of, write_yaml


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_like_mask_computes(parquet_path, extra_config):
    _write(
        extra_config,
        94010,
        measures={
            "late": {
                "of": "sotif_value",
                "op": "point",
                "where": {"column": "reason_code", "op": "like", "value": "LATE%"},
            }
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["late"],
        supplier=["ABC"],
        kpi_id=94010,
        month="2026-03",
    )
    late = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    other = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="OTHER")
    assert value_of(late, "late") == 45.0
    assert other["late"] is None


def test_or_and_not_masks(parquet_path, extra_config):
    _write(
        extra_config,
        94011,
        measures={
            "either": {
                "of": "sotif_value",
                "op": "point",
                "where": {
                    "or": [
                        {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                        {"column": "reason_code", "op": "eq", "value": "OTHER"},
                    ]
                },
            },
            "not_other": {
                "of": "sotif_value",
                "op": "point",
                "where": {
                    "not": {"column": "reason_code", "op": "eq", "value": "OTHER"}
                },
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["either", "not_other"],
        supplier=["ABC"],
        kpi_id=94011,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")
    assert value_of(late, "either") == pytest.approx(value_of(late, "not_other"))
    assert value_of(other, "either") == 6.0
    assert other["not_other"] is None


def test_also_where_and_where_and_are_equivalent(extra_config):
    _write(
        extra_config,
        94012,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "via_and": {
                "sql": "amount",
                "agg": "sum",
                "where": {
                    "and": [
                        {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                        {"column": "region", "op": "eq", "value": "NA"},
                    ]
                },
            },
            "via_also": {
                "sql": "amount",
                "agg": "sum",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "also_where": [{"column": "region", "op": "eq", "value": "NA"}],
            },
        },
        measures={
            "and_point": {"of": "via_and", "op": "point"},
            "also_point": {"of": "via_also", "op": "point"},
        },
    )
    kpi = load_kpi(94012, extra_config)
    by_name = {b.name: b for b in kpi.base_measures}
    assert by_name["via_and"].where.kind == "leaf"
    assert by_name["via_and"].where.column == "reason_code"
    assert len(by_name["via_and"].also_where) == 1
    assert by_name["via_also"].also_where[0].column == "region"


def test_regexp_and_invalid_pattern(extra_config):
    _write(
        extra_config,
        94013,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "ok": {
                "sql": "amount",
                "agg": "sum",
                "where": {"column": "reason_code", "op": "regexp", "value": "LATE"},
            }
        },
    )
    kpi = load_kpi(94013, extra_config)
    by_name = {b.name: b for b in kpi.base_measures}
    assert by_name["ok"].where.op == "regexp"

    _write(
        extra_config,
        94014,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "bad": {
                "sql": "amount",
                "agg": "sum",
                "where": {"column": "reason_code", "op": "regexp", "value": "("},
            }
        },
    )
    with pytest.raises(BindError, match="invalid regexp"):
        load_kpi(94014, extra_config)


def test_empty_or_and_depth_and_is_null_extra_value(extra_config):
    _write(
        extra_config,
        94015,
        base_measures={
            "empty": {
                "sql": "amount",
                "agg": "sum",
                "where": {"or": []},
            }
        },
    )
    with pytest.raises(BindError, match="non-empty"):
        load_kpi(94015, extra_config)

    _write(
        extra_config,
        94016,
        base_measures={
            "nullish": {
                "sql": "amount",
                "agg": "sum",
                "where": {"column": "region", "op": "is_null", "value": "x"},
            }
        },
    )
    with pytest.raises(BindError, match="does not take value"):
        load_kpi(94016, extra_config)

    deep = {"column": "reason_code", "op": "eq", "value": "X"}
    for _ in range(4):
        deep = {"not": deep}
    _write(
        extra_config,
        94017,
        base_measures={"deep": {"sql": "amount", "agg": "sum", "where": deep}},
    )
    with pytest.raises(BindError, match="nesting exceeds"):
        load_kpi(94017, extra_config)


def test_snapshot_filtered_point_with_mask(parquet_path, extra_config):
    _write(
        extra_config,
        94018,
        time=None,
        measures={
            "late": {
                "op": "filtered_point",
                "column": "amount",
                "where": {
                    "or": [
                        {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                    ]
                },
            }
        },
    )
    kpi = load_kpi(94018, extra_config)
    spec = {m.key: m for m in kpi.measures}["late"]
    assert spec.kind == "point"
    ctx = make_context(
        parquet_path,
        measures=["late"],
        supplier=["ABC"],
        kpi_id=94018,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "late") is not None
