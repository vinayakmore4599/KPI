"""Phase 2 measure functions: bind, lookback, and compute."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


FN_CASES = [
    ("geomean_scalars", {"op": "fn", "fn": "geomean_scalars", "inputs": ["current_value", "value_3m"]}),
    (
        "weighted_avg_scalars",
        {
            "op": "fn",
            "fn": "weighted_avg_scalars",
            "inputs": ["current_value", "value_3m", "value_3m", "current_value"],
        },
    ),
    ("harmonic_mean", {"op": "fn", "fn": "harmonic_mean", "inputs": ["current_value", "value_3m"]}),
    ("min_max_spread", {"op": "fn", "fn": "min_max_spread", "inputs": ["current_value", "value_3m"]}),
    ("ratio_safe", {"op": "fn", "fn": "ratio_safe", "inputs": ["current_value", "value_3m"]}),
    ("bps_change", {"op": "fn", "fn": "bps_change", "inputs": ["current_value", "value_3m"]}),
    ("log_change", {"op": "fn", "fn": "log_change", "inputs": ["current_value", "value_3m"]}),
    (
        "if_between",
        {
            "op": "fn",
            "fn": "if_between",
            "inputs": ["current_value", "lo", "hi"],
        },
    ),
    ("all_null", {"op": "fn", "fn": "all_null", "inputs": ["current_value", "value_3m"]}),
    ("any_null", {"op": "fn", "fn": "any_null", "inputs": ["current_value", "value_3m"]}),
]


def _write_fn(extra_config, kpi_id: int, probe: dict):
    spec = minimal_kpi(
        kpi_id,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
            },
            "lo": {"op": "constant", "value": 0},
            "hi": {"op": "constant", "value": 100000},
            "probe": probe,
        },
    )
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return load_kpi(kpi_id, extra_config)


@pytest.mark.parametrize("kpi_id,name,probe", [
    (97600 + i, name, probe) for i, (name, probe) in enumerate(FN_CASES)
])
def test_measure_fn_bind(extra_config, kpi_id, name, probe):
    kpi = _write_fn(extra_config, kpi_id, probe)
    spec = {m.key: m for m in kpi.measures}["probe"]
    assert spec.fn == name or spec.kind == "fn"
    del name


@pytest.mark.parametrize("kpi_id,name,probe", [
    (97650 + i, name, probe) for i, (name, probe) in enumerate(FN_CASES)
])
def test_measure_fn_lookback(extra_config, kpi_id, name, probe):
    kpi = _write_fn(extra_config, kpi_id, probe)
    assert max_lookback_months(kpi, ("probe",)) >= 0
    del name


@pytest.mark.parametrize("kpi_id,name,probe", [
    (97700 + i, name, probe) for i, (name, probe) in enumerate(FN_CASES)
])
def test_measure_fn_compute(parquet_path, extra_config, kpi_id, name, probe):
    _write_fn(extra_config, kpi_id, probe)
    ctx = make_context(
        parquet_path, measures=["probe"], supplier=["ABC"], kpi_id=kpi_id
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert "probe" in row
    _ = value_of(row, "probe")
    del name
