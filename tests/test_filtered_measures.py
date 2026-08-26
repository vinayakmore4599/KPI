"""Bind-time filtered_* sugar — column vs of, clone matrix, bind-error catalog."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.adapter import adapt
from kpi_engine.pipeline.binder import bind_request, load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_filtered_point_of_matches_point_where(parquet_path, extra_config):
    _write(
        extra_config,
        99201,
        measures={
            "legacy": {
                "of": "sotif_value",
                "op": "point",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
            "sugar": {
                "op": "filtered_point",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["legacy", "sugar"],
        supplier=["ABC"],
        kpi_id=99201,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    other = find_row(result, cut="G", reason="OTHER")
    assert other["legacy"] is None
    assert other["sugar"] is None
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "sugar") == pytest.approx(value_of(late, "legacy"))
    assert value_of(late, "sugar") == 45.0


def test_filtered_point_column_path_binds_without_named_base(extra_config):
    _write(
        extra_config,
        99202,
        measures={
            "closed_amount": {
                "op": "filtered_point",
                "column": "amount",
                "agg": "sum",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    kpi = load_kpi(99202, extra_config)
    spec = {m.key: m for m in kpi.measures}["closed_amount"]
    assert spec.kind == "point"
    assert spec.of == "__closed_amount__base"
    assert spec.where is None
    base = {b.name: b for b in kpi.base_measures}["__closed_amount__base"]
    assert base.sql == "amount"
    assert base.agg == "sum"
    assert base.where is not None
    assert base.where.column == "reason_code"


def test_filtered_point_column_computes(parquet_path, extra_config):
    _write(
        extra_config,
        99203,
        measures={
            "late_amt": {
                "op": "filtered_point",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["late_amt"],
        supplier=["ABC"],
        kpi_id=99203,
        month="2026-03",
    )
    late = find_row(
        compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER"
    )
    assert value_of(late, "late_amt") == 45.0


def test_column_dedupe_shared_by_point_and_compare(extra_config):
    _write(
        extra_config,
        99204,
        measures={
            "late_now": {
                "op": "filtered_point",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
            "late_yoy": {
                "op": "filtered_compare",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "mode": "yoy",
            },
        },
    )
    kpi = bind_request(load_kpi(99204, extra_config))
    synthetics = [b.name for b in kpi.base_measures if b.name.startswith("__") and b.name.endswith("__base")]
    assert len(synthetics) == 1
    shared = synthetics[0]
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["late_now"].kind == "point"
    assert by_key["late_now"].of == shared
    assert by_key["late_yoy"].kind == "pct_change"
    assert by_key["late_yoy"].of == shared
    assert by_key["late_yoy"].offset.years == 1


def test_ignore_filters_does_not_split_column_dedupe(extra_config):
    _write(
        extra_config,
        99205,
        measures={
            "masked": {
                "op": "filtered_point",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
            "worldwide": {
                "op": "filtered_point",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "ignore_filters": ["region"],
            },
        },
    )
    kpi = load_kpi(99205, extra_config)
    synthetics = [b.name for b in kpi.base_measures if b.name.endswith("__base")]
    assert len(synthetics) == 1
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["worldwide"].ignore_filters == ("region",)
    assert by_key["worldwide"].of == "__worldwide__of"


def test_filtered_window_desugars(extra_config):
    _write(
        extra_config,
        99206,
        measures={
            "closed_3m": {
                "op": "filtered_window",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "trailing": {"months": 3},
                "inclusive": True,
            }
        },
    )
    kpi = load_kpi(99206, extra_config)
    spec = {m.key: m for m in kpi.measures}["closed_3m"]
    assert spec.kind == "window"
    assert spec.trailing_months == 3
    assert spec.inclusive is True


def test_filtered_trend_desugars_and_keeps_cuts(extra_config):
    _write(
        extra_config,
        99207,
        measures={
            "closed_trend": {
                "op": "filtered_trend",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "trailing": {"months": 12},
                "inclusive": True,
                "cuts": ["G"],
            }
        },
    )
    kpi = load_kpi(99207, extra_config)
    spec = {m.key: m for m in kpi.measures}["closed_trend"]
    assert spec.kind == "trend"
    assert spec.cuts == ("G",)


def test_filtered_compare_computes_yoy(parquet_path, extra_config):
    _write(
        extra_config,
        99208,
        measures={
            "late_yoy": {
                "op": "filtered_compare",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "mode": "yoy",
            }
        },
    )
    kpi = bind_request(load_kpi(99208, extra_config))
    spec = {m.key: m for m in kpi.measures}["late_yoy"]
    assert spec.kind == "pct_change"
    ctx = make_context(
        parquet_path,
        measures=["late_yoy"],
        supplier=["ABC"],
        kpi_id=99208,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "late_yoy") is not None


def test_filtered_compare_lookback(extra_config):
    _write(
        extra_config,
        99209,
        measures={
            "late_yoy": {
                "op": "filtered_compare",
                "column": "amount",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "mode": "yoy",
            }
        },
    )
    kpi = bind_request(load_kpi(99209, extra_config))
    assert max_lookback_months(kpi, ("late_yoy",)) == 12


def test_filtered_point_snapshot_ok(extra_config):
    _write(
        extra_config,
        99210,
        time=None,
        measures={
            "late_amt": {
                "op": "filtered_point",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    kpi = load_kpi(99210, extra_config)
    spec = {m.key: m for m in kpi.measures}["late_amt"]
    assert spec.kind == "point"


def test_filtered_window_snapshot_rejected(extra_config):
    _write(
        extra_config,
        99211,
        time=None,
        measures={
            "w": {
                "op": "filtered_window",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
                "trailing": {"months": 3},
            }
        },
    )
    with pytest.raises(BindError, match="time"):
        load_kpi(99211, extra_config)


def test_column_and_of_is_bind_error(extra_config):
    _write(
        extra_config,
        99220,
        measures={
            "x": {
                "op": "filtered_point",
                "column": "amount",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    with pytest.raises(BindError, match="column") as exc:
        load_kpi(99220, extra_config)
    assert "of" in str(exc.value)


def test_missing_where_is_bind_error(extra_config):
    _write(
        extra_config,
        99221,
        measures={"x": {"op": "filtered_point", "of": "sotif_value"}},
    )
    with pytest.raises(BindError, match="where"):
        load_kpi(99221, extra_config)


def test_filtered_of_derived_measure_is_bind_error(extra_config):
    _write(
        extra_config,
        99222,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "masked": {
                "op": "filtered_point",
                "of": "current_value",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
        },
    )
    with pytest.raises(BindError, match="requires of: a base"):
        load_kpi(99222, extra_config)


def test_authored_dunder_key_is_reserved(extra_config):
    _write(
        extra_config,
        99223,
        measures={"__hidden": {"of": "sotif_value", "op": "point"}},
    )
    with pytest.raises(BindError, match="reserved"):
        load_kpi(99223, extra_config)


def test_authored_dunder_base_is_reserved(extra_config):
    spec = minimal_kpi(
        99226,
        base_measures={
            "__hidden": {"sql": "amount", "agg": "sum"},
            "sotif_value": {"sql": "amount", "agg": "sum"},
        },
    )
    write_yaml(extra_config / "kpis" / "99226.yaml", spec)
    with pytest.raises(BindError, match="reserved"):
        load_kpi(99226, extra_config)


def test_agg_on_of_path_is_bind_error(extra_config):
    _write(
        extra_config,
        99224,
        measures={
            "x": {
                "op": "filtered_point",
                "of": "sotif_value",
                "agg": "sum",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    with pytest.raises(BindError, match="agg"):
        load_kpi(99224, extra_config)


def test_filtered_model_key_rejected(extra_config):
    _write(
        extra_config,
        99225,
        measures={
            "x": {
                "op": "filtered_point",
                "column": "amount",
                "model": "sotif",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    with pytest.raises(BindError, match="model"):
        load_kpi(99225, extra_config)


def test_filtered_point_unknown_column_fails_validate(parquet_path, extra_config):
    from kpi_engine import validate

    _write(
        extra_config,
        99227,
        measures={
            "x": {
                "op": "filtered_point",
                "column": "no_such_col",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    ctx = make_context(
        parquet_path, measures=["x"], supplier=["ABC"], kpi_id=99227, month="2026-03"
    )
    with pytest.raises(BindError, match="column"):
        validate(ctx, config_dir=extra_config)
