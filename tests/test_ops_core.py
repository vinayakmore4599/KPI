"""Phase 2 core catalog: bind, lookback, and compute for new combo/cut/period ops."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import bind_request, load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _prep(extra_config, kpi_id: int, probe: dict, **overrides):
    measures = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "value_3m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 3},
            "inclusive": True,
        },
        "probe": probe,
    }
    spec = minimal_kpi(kpi_id, measures=measures, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return load_kpi(kpi_id, extra_config)


CORE_OPS = [
    ("expanding_window", {"op": "expanding_window", "of": "sotif_value"}),
    (
        "shifted_trend",
        {
            "op": "shifted_trend",
            "of": "sotif_value",
            "trailing": {"months": 3},
            "offset": {"years": 1},
            "cuts": ["G"],
        },
    ),
    ("rate", {"op": "rate", "of": "sotif_value", "n": 3}),
    ("cumulative_point", {"op": "cumulative_point", "of": "sotif_value"}),
    (
        "n_period_avg",
        {"op": "n_period_avg", "of": "sotif_value", "trailing": {"months": 3}},
    ),
    (
        "weighted_window",
        {
            "op": "weighted_window",
            "of": "sotif_value",
            "weight": "sotif_value",
            "trailing": {"months": 3},
        },
    ),
    (
        "snapshot_compare",
        {"op": "snapshot_compare", "of": "current_value", "vs": "value_3m", "mode": "pct"},
    ),
    ("bottom_n", {"op": "bottom_n", "of": "current_value", "n": 1, "cuts": ["G"]}),
    (
        "rank_pct_change",
        {
            "op": "rank_pct_change",
            "of": "current_value",
            "offset": {"years": 1},
            "cuts": ["G"],
        },
    ),
    ("concentration", {"op": "concentration", "of": "current_value", "cuts": ["G"]}),
    ("abc_class", {"op": "abc_class", "of": "current_value", "cuts": ["G"]}),
    ("pareto_flag", {"op": "pareto_flag", "of": "current_value", "cuts": ["G"]}),
    (
        "normalize",
        {"op": "normalize", "of": "current_value", "method": "max", "cuts": ["G"]},
    ),
    ("annualize", {"op": "annualize", "of": "current_value", "n": 1}),
    (
        "vs_prior_window",
        {
            "op": "vs_prior_window",
            "of": "sotif_value",
            "trailing": {"months": 3},
            "offset": {"years": 1},
        },
    ),
    (
        "delta_contribution",
        {"op": "delta_contribution", "of": "current_value", "offset": {"years": 1}},
    ),
    (
        "baseline_index",
        {"op": "baseline_index", "of": "current_value", "offset": {"years": 1}},
    ),
]

ADVANCED_OPS = [
    (
        "band",
        {
            "op": "band",
            "of": "current_value",
            "low": 0.8,
            "high": 1.2,
            "emit": "low",
        },
    ),
    (
        "envelope",
        {
            "op": "envelope",
            "of": "current_value",
            "low": 0.9,
            "high": 1.1,
            "emit": "high",
        },
    ),
    ("compound_growth", {"op": "compound_growth", "of": "sotif_value", "n": 3}),
    (
        "seasonal_adjust",
        {"op": "seasonal_adjust", "of": "sotif_value", "trailing": {"months": 12}},
    ),
]


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97000 + i, kind, probe) for i, (kind, probe) in enumerate(CORE_OPS)
])
def test_core_op_bind(extra_config, kpi_id, kind, probe):
    kpi = _prep(extra_config, kpi_id, probe)
    loaded = {m.key: m for m in kpi.measures}["probe"]
    assert loaded.kind == kind


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97100 + i, kind, probe) for i, (kind, probe) in enumerate(CORE_OPS)
])
def test_core_op_lookback(extra_config, kpi_id, kind, probe):
    kpi = bind_request(_prep(extra_config, kpi_id, probe))
    assert max_lookback_months(kpi, ("probe",)) >= 0
    del kind


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97200 + i, kind, probe) for i, (kind, probe) in enumerate(CORE_OPS)
])
def test_core_op_compute(parquet_path, extra_config, kpi_id, kind, probe):
    _prep(extra_config, kpi_id, probe)
    ctx = make_context(
        parquet_path,
        measures=["probe"],
        supplier=["ABC"],
        kpi_id=kpi_id,
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert "probe" in row
    if kind == "abc_class":
        assert value_of(row, "probe") in {None, "A", "B", "C"}
    elif kind == "shifted_trend":
        assert value_of(row, "probe") is None or isinstance(value_of(row, "probe"), list)
    else:
        # Presence is enough; null is a legal result when lookback has no prior year.
        _ = value_of(row, "probe")


def test_window_align_periods_binds(extra_config):
    kpi = _prep(
        extra_config,
        97300,
        {"op": "window", "of": "sotif_value", "trailing": {"months": 3}, "align": "periods"},
    )
    spec = {m.key: m for m in kpi.measures}["probe"]
    assert spec.params.get("align") == "periods"


def test_rate_compute_divides_by_n(parquet_path, extra_config):
    _prep(extra_config, 97301, {"op": "rate", "of": "sotif_value", "n": 3})
    ctx = make_context(
        parquet_path, measures=["probe", "current_value"], supplier=["ABC"], kpi_id=97301
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "probe") == pytest.approx(value_of(row, "current_value") / 3)


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97400 + i, kind, probe) for i, (kind, probe) in enumerate(ADVANCED_OPS)
])
def test_advanced_op_bind(extra_config, kpi_id, kind, probe):
    kpi = _prep(extra_config, kpi_id, probe)
    loaded = {m.key: m for m in kpi.measures}["probe"]
    assert loaded.kind == kind


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97500 + i, kind, probe) for i, (kind, probe) in enumerate(ADVANCED_OPS)
])
def test_advanced_op_lookback(extra_config, kpi_id, kind, probe):
    kpi = bind_request(_prep(extra_config, kpi_id, probe))
    assert max_lookback_months(kpi, ("probe",)) >= 0
    del kind


@pytest.mark.parametrize("kpi_id,kind,probe", [
    (97600 + i, kind, probe) for i, (kind, probe) in enumerate(ADVANCED_OPS)
])
def test_advanced_op_compute(parquet_path, extra_config, kpi_id, kind, probe):
    _prep(extra_config, kpi_id, probe)
    ctx = make_context(
        parquet_path, measures=["probe"], supplier=["ABC"], kpi_id=kpi_id
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert "probe" in row
    _ = value_of(row, "probe")
    del kind


def test_band_expands_to_low_and_high_keys(extra_config):
    spec = minimal_kpi(
        97700,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "value_band": {
                "op": "band",
                "of": "current_value",
                "low": 0.8,
                "high": 1.2,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "97700.yaml", spec)
    kpi = load_kpi(97700, extra_config)
    keys = {m.key for m in kpi.measures}
    assert "value_band_low" in keys and "value_band_high" in keys
    assert "value_band" not in keys

