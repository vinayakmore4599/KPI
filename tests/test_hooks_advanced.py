"""Phase 3 series / cohort hooks: bind, lookback, compute."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import bind_request, load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of

HOOKS = [
    ("autocorrelation", {"op": "hook", "hook": "autocorrelation", "of": "sotif_value", "trailing": {"months": 12}}),
    ("rsi", {"op": "hook", "hook": "rsi", "of": "sotif_value", "trailing": {"months": 12}}),
    ("bollinger", {"op": "hook", "hook": "bollinger", "of": "sotif_value", "trailing": {"months": 12}, "k": 2}),
    ("exponential_decay_sum", {"op": "hook", "hook": "exponential_decay_sum", "of": "sotif_value", "trailing": {"months": 12}, "decay": 0.5}),
    ("theil_sen_slope", {"op": "hook", "hook": "theil_sen_slope", "of": "sotif_value", "trailing": {"months": 12}}),
    ("changepoint", {"op": "hook", "hook": "changepoint", "of": "sotif_value", "trailing": {"months": 12}}),
    ("outlier_count", {"op": "hook", "hook": "outlier_count", "of": "sotif_value", "trailing": {"months": 12}}),
    ("percentile_rank_series", {"op": "hook", "hook": "percentile_rank_series", "of": "sotif_value", "trailing": {"months": 12}}),
    ("weighted_ewma", {"op": "hook", "hook": "weighted_ewma", "of": "sotif_value", "trailing": {"months": 12}, "alpha": 0.3}),
    ("run_rate", {"op": "hook", "hook": "run_rate", "of": "sotif_value", "trailing": {"months": 3}}),
    ("target_trajectory", {"op": "hook", "hook": "target_trajectory", "of": "sotif_value", "trailing": {"months": 12}, "value": 100}),
    ("forecast_confidence", {"op": "hook", "hook": "forecast_confidence", "of": "sotif_value", "trailing": {"months": 12}, "emit": "high"}),
    ("seasonal_decompose", {"op": "hook", "hook": "seasonal_decompose", "of": "sotif_value", "trailing": {"months": 12}}),
    ("cohort_retention", {"op": "hook", "hook": "cohort_retention", "of": "sotif_value", "trailing": {"months": 12}, "cohort_column": "supplier_name", "entry_period": "2025-01-01"}),
    ("survival_rate", {"op": "hook", "hook": "survival_rate", "of": "sotif_value", "trailing": {"months": 12}, "cohort_column": "supplier_name", "entry_period": "2025-01-01"}),
]


def _prep(extra_config, kpi_id: int, probe: dict):
    spec = minimal_kpi(
        kpi_id,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "probe": probe,
        },
    )
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return load_kpi(kpi_id, extra_config)


@pytest.mark.parametrize("kpi_id,name,probe", [
    (98100 + i, name, probe) for i, (name, probe) in enumerate(HOOKS)
])
def test_advanced_hook_bind(extra_config, kpi_id, name, probe):
    kpi = _prep(extra_config, kpi_id, probe)
    spec = {m.key: m for m in kpi.measures}["probe"]
    assert spec.kind == "hook"
    assert spec.hook == name


@pytest.mark.parametrize("kpi_id,name,probe", [
    (98200 + i, name, probe) for i, (name, probe) in enumerate(HOOKS)
])
def test_advanced_hook_lookback(extra_config, kpi_id, name, probe):
    kpi = bind_request(_prep(extra_config, kpi_id, probe))
    assert max_lookback_months(kpi, ("probe",)) >= 0
    del name


@pytest.mark.parametrize("kpi_id,name,probe", [
    (98300 + i, name, probe) for i, (name, probe) in enumerate(HOOKS)
])
def test_advanced_hook_compute(parquet_path, extra_config, kpi_id, name, probe):
    _prep(extra_config, kpi_id, probe)
    ctx = make_context(
        parquet_path, measures=["probe"], supplier=["ABC"], kpi_id=kpi_id
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert "probe" in row
    _ = value_of(row, "probe")
    del name


def test_forecast_confidence_expands_dual_keys(extra_config):
    spec = minimal_kpi(
        98400,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "fc": {
                "op": "hook",
                "hook": "forecast_confidence",
                "of": "sotif_value",
                "trailing": {"months": 12},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "98400.yaml", spec)
    kpi = load_kpi(98400, extra_config)
    keys = {m.key for m in kpi.measures}
    assert "fc_low" in keys and "fc_high" in keys
    assert "fc" not in keys
