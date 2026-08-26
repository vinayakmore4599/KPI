"""Lookback matrix. Phase 2–3 gate: every new core/advanced op reports a finite lookback."""

from __future__ import annotations

import pytest

from kpi_engine.pipeline.binder import bind_request, load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import minimal_kpi, write_yaml
from tests.test_ops_core import ADVANCED_OPS, CORE_OPS


def test_compare_wow_at_week_grain_lookback_is_one_period(extra_config):
    spec = minimal_kpi(
        94301,
        time={
            "column": "event_month",
            "grain": "week",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
            "grains": ["week", "month"],
        },
        parameters={"time_grain": {"type": "string", "default": "week"}},
        measures={"wow": {"op": "compare", "of": "sotif_value", "mode": "wow"}},
    )
    write_yaml(extra_config / "kpis" / "94301.yaml", spec)
    kpi = bind_request(load_kpi(94301, extra_config, parameters={"time_grain": "week"}))
    assert kpi.time.grain == "week"
    chg = {m.key: m for m in kpi.measures}["wow"]
    assert chg.kind == "pct_change"
    assert chg.offset.weeks == 1
    assert max_lookback_months(kpi, ("wow",)) >= 1


@pytest.mark.parametrize("idx,kind,probe", [(i, k, p) for i, (k, p) in enumerate(CORE_OPS)])
def test_core_op_lookback_matrix(extra_config, idx, kind, probe):
    spec = minimal_kpi(
        94400 + idx,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
            },
            "probe": probe,
        },
    )
    write_yaml(extra_config / "kpis" / f"{spec['kpi_id']}.yaml", spec)
    kpi = bind_request(load_kpi(spec["kpi_id"], extra_config))
    assert max_lookback_months(kpi, ("probe",)) >= 0
    assert {m.key: m for m in kpi.measures}["probe"].kind == kind


@pytest.mark.parametrize("idx,kind,probe", [(i, k, p) for i, (k, p) in enumerate(ADVANCED_OPS)])
def test_advanced_op_lookback_matrix(extra_config, idx, kind, probe):
    spec = minimal_kpi(
        94500 + idx,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "probe": probe,
        },
    )
    write_yaml(extra_config / "kpis" / f"{spec['kpi_id']}.yaml", spec)
    kpi = bind_request(load_kpi(spec["kpi_id"], extra_config))
    assert max_lookback_months(kpi, ("probe",)) >= 0
    assert {m.key: m for m in kpi.measures}["probe"].kind == kind
