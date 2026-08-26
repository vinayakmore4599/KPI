"""New calculation capabilities: aggs, hooks, composition, time opt-ins."""

import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError, TimePlanError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, unwrap_cell, value_of


def _gaps_kpi(kpi_id: int, **overrides):
    spec = minimal_kpi(
        kpi_id,
        time={
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
            "periods": {"year": "year", "month": "current month"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
        },
    )
    spec.update(overrides)
    if "time" in overrides:
        spec["time"] = overrides["time"]
    if "measures" in overrides:
        spec["measures"] = overrides["measures"]
    if "base_measures" in overrides:
        spec["base_measures"] = overrides["base_measures"]
    return spec


def test_week_offset_binds_on_period_ops(extra_config):
    spec = minimal_kpi(
        97001,
        time={
            "column": "event_month",
            "grain": "week",
            "source_grain": "week",
            "filter_code": "reporting_month",
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "lagged": {"op": "lag", "of": "current_value", "offset": {"weeks": 1}},
            "yoy": {"op": "pct_change", "of": "current_value", "offset": {"weeks": 52}},
        },
    )
    write_yaml(extra_config / "kpis" / "97001.yaml", spec)
    kpi = load_kpi(97001, extra_config)
    assert {m.key for m in kpi.measures} >= {"lagged", "yoy"}


def test_stddev_variance_mode_compute(parquet_path, extra_config):
    spec = _gaps_kpi(
        97002,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "spread": {"sql": "amount", "agg": "stddev"},
            "var_amt": {"sql": "amount", "agg": "variance"},
            "typical": {"sql": "amount", "agg": "mode"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "spread_now": {"of": "spread", "op": "point"},
            "var_now": {"of": "var_amt", "op": "point"},
            "mode_now": {"of": "typical", "op": "point"},
        },
    )
    write_yaml(extra_config / "kpis" / "97002.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["spread_now", "var_now", "mode_now"],
        supplier=["ABC"],
        kpi_id=97002,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["spread_now"] == pytest.approx(10.6066017178, rel=1e-6)
    assert g["var_now"] == pytest.approx(112.5, rel=1e-6)
    assert unwrap_cell(g["mode_now"]) in (15.0, 30.0)


def test_mad_projection_rolling_median(parquet_path, extra_config):
    spec = _gaps_kpi(
        97003,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "robust": {
                "op": "hook",
                "hook": "mad",
                "of": "sotif_value",
                "trailing": {"months": 12},
            },
            "next": {
                "op": "hook",
                "hook": "projection",
                "of": "sotif_value",
                "trailing": {"months": 12},
                "periods_ahead": 1,
            },
            "typical": {
                "op": "hook",
                "hook": "rolling_median",
                "of": "sotif_value",
                "trailing": {"months": 12},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97003.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["robust", "next", "typical"],
        supplier=["ABC"],
        kpi_id=97003,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["robust"] is not None
    assert g["next"] is not None
    assert g["typical"] is not None


def test_lag_of_hook_computes(parquet_path, extra_config):
    spec = _gaps_kpi(
        97004,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "smoothed": {
                "op": "hook",
                "hook": "ewma",
                "of": "sotif_value",
                "trailing": {"months": 3},
            },
            "lagged": {"op": "lag", "of": "smoothed", "offset": {"months": 1}},
        },
    )
    write_yaml(extra_config / "kpis" / "97004.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["smoothed", "lagged"],
        supplier=["ABC"],
        kpi_id=97004,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["smoothed"] is not None
    assert g["lagged"] is not None
    assert g["lagged"] != g["smoothed"]


def test_trend_offset_shifts_axis(parquet_path, extra_config):
    spec = _gaps_kpi(
        97005,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "trend_ly": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 12},
                "inclusive": True,
                "offset": {"years": 1},
                "cuts": ["G"],
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97005.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["trend_ly"],
        supplier=["ABC"],
        kpi_id=97005,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    axis = result["trend_axes"]["trend_ly"]
    assert axis[0] == "2024-04-01"
    assert axis[-1] == "2025-03-01"


def test_rank_of_lag_binds(extra_config):
    spec = minimal_kpi(
        97006,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "prior": {"op": "lag", "of": "current_value", "offset": {"months": 1}},
            "rank_prior": {"op": "rank", "of": "prior", "order": "desc", "cuts": ["G"]},
        },
    )
    write_yaml(extra_config / "kpis" / "97006.yaml", spec)
    kpi = load_kpi(97006, extra_config)
    assert any(m.key == "rank_prior" for m in kpi.measures)


def test_percent_of_total_times_current_computes(parquet_path, extra_config):
    spec = _gaps_kpi(
        97007,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "share": {
                "op": "percent_of_total",
                "of": "current_value",
                "cuts": ["G"],
            },
            "weighted": {
                "op": "arithmetic",
                "fn": "multiply",
                "left": "share",
                "right": "current_value",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97007.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["share", "weighted"],
        supplier=["ABC"],
        kpi_id=97007,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["share"] is not None
    assert g["weighted"] == pytest.approx(g["share"] * 45.0)


def test_versus_cut_share(parquet_path, extra_config):
    spec = _gaps_kpi(
        97008,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "share_of_g": {
                "op": "percent_of_total",
                "of": "current_value",
                "versus_cut": "G",
                "cuts": ["R"],
            },
        },
        parameters={"output_cut": {"type": "string", "allowed": ["G", "R"]}},
    )
    write_yaml(extra_config / "kpis" / "97008.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "share_of_g"],
        supplier=["ABC"],
        kpi_id=97008,
        month="2026-03",
        parameters={"output_cut": "R"},
    )
    result = compute(ctx, config_dir=extra_config)
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    # G LATE_SUPPLIER + OTHER at Mar 2026 is 45 + 6 = 51; NA LATE is 30.
    assert na["share_of_g"] == pytest.approx(30.0 * 100.0 / 51.0)


def test_measure_where_filters_one_measure(parquet_path, extra_config):
    spec = _gaps_kpi(
        97009,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "late_only": {
                "of": "sotif_value",
                "op": "point",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97009.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "late_only"],
        supplier=["ABC"],
        kpi_id=97009,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    other = find_row(result, cut="G", reason="OTHER")
    assert value_of(other, "current_value") == 6.0
    assert other["late_only"] is None
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "late_only") == 45.0


def test_measure_ignore_filters_skips_region(parquet_path, extra_config):
    spec = _gaps_kpi(
        97012,
        cuts=[{"name": "G", "group_by": [], "ignore_filters": []}],
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "worldwide": {
                "of": "sotif_value",
                "op": "point",
                "ignore_filters": ["region"],
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97012.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "worldwide"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=97012,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(g, "current_value") == 30.0
    assert value_of(g, "worldwide") == 45.0
    assert any(
        row.get("reason") == "measure_worldwide" and row.get("filter_code") == "region"
        for row in result["ignored_filters"]
    )


def test_measure_where_on_derived_is_bind_error(extra_config):
    spec = _gaps_kpi(
        97013,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "masked": {
                "of": "current_value",
                "op": "point",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97013.yaml", spec)
    with pytest.raises(BindError, match="requires of: a base"):
        load_kpi(97013, extra_config)


def test_last_observed_clamps_mid_year(parquet_path, extra_config):
    spec = _gaps_kpi(
        97010,
        time={
            "column": "event_month",
            "grain": "month",
            "calendar": "gregorian",
            "anchor": "last_observed",
            "periods": {"year": "year"},
        },
    )
    write_yaml(extra_config / "kpis" / "97010.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=97010,
    )
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["year"] = {"value": [2026], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] == "2026-03-01"
    assert not any(n.get("code") == "unobserved_anchor" for n in result["notes"])
    planned = validate(ctx, config_dir=extra_config)
    assert planned["anchor"] is None
    assert planned["time_selection"]["anchor_source"] == "data"


def test_max_span_years_raises(parquet_path, extra_config):
    spec = _gaps_kpi(
        97011,
        time={
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "max_span_years": 1,
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "trend_12m": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 24},
                "inclusive": True,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97011.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "trend_12m"],
        supplier=["ABC"],
        kpi_id=97011,
        month="2026-03",
    )
    with pytest.raises(TimePlanError, match="max_span_years"):
        compute(ctx, config_dir=extra_config)
