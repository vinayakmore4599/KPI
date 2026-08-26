"""End-to-end compute: new add-on ops and hooks on one KPI through DuckDB + Pandas."""

from __future__ import annotations

from kpi_engine import compute, validate
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _addon_kpi(kpi_id: int) -> dict:
    spec = minimal_kpi(kpi_id)
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"years": 1},
    }
    spec["measures"]["target"] = {"op": "constant", "value": 40}
    spec["measures"]["value_3m_ly"] = {"op": "lag", "of": "value_3m", "offset": {"years": 1}}
    spec["measures"]["volume_index"] = {
        "op": "index",
        "of": "current_value",
        "offset": {"years": 1},
    }
    spec["measures"]["gap"] = {
        "op": "vs_target",
        "of": "current_value",
        "vs": "target",
        "as": "gap",
    }
    spec["measures"]["hit"] = {
        "op": "threshold",
        "of": "current_value",
        "cmp": "gte",
        "value": 40,
    }
    spec["measures"]["vs_goal"] = {
        "op": "fn",
        "fn": "attainment",
        "inputs": ["current_value", "target"],
    }
    spec["measures"]["q"] = {"op": "ntile", "of": "current_value", "tiles": 4, "order": "desc"}
    spec["measures"]["contrib"] = {
        "op": "contribution",
        "of": "current_value",
        "vs": "previous_year_value",
    }
    spec["measures"]["smoothed"] = {
        "op": "hook",
        "hook": "ewma",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["on_bar"] = {
        "op": "hook",
        "hook": "hit_rate",
        "of": "sotif_value",
        "trailing": {"months": 3},
        "value": 30,
    }
    spec["measures"]["held"] = {
        "op": "hook",
        "hook": "streak",
        "of": "sotif_value",
        "trailing": {"months": 3},
        "value": 30,
    }
    spec["measures"]["seasonal"] = {
        "op": "hook",
        "hook": "seasonal_index",
        "of": "sotif_value",
        "trailing": {"months": 15},
    }
    return spec


def test_e2e_validate_then_compute_addon_catalog(parquet_path, extra_config):
    """One request: compile SQL, extract, then calculate new ops and hooks together."""
    write_yaml(extra_config / "kpis" / "9910.yaml", _addon_kpi(9910))
    measures = [
        "current_value",
        "value_3m",
        "value_3m_ly",
        "volume_index",
        "gap",
        "hit",
        "vs_goal",
        "q",
        "contrib",
        "smoothed",
        "on_bar",
        "held",
        "seasonal",
    ]
    ctx = make_context(parquet_path, measures=measures, supplier=["ABC"], kpi_id=9910)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"]
    assert "read_parquet" in planned["sql"].lower() or "parquet" in planned["sql"].lower()
    assert planned["lookback_months"] >= 12
    assert "rows" not in planned

    result = compute(ctx, config_dir=extra_config)
    assert result["sql"]
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")

    assert value_of(late, "current_value") == 45.0
    assert value_of(late, "value_3m") == 90.0
    assert value_of(late, "value_3m_ly") == 60.0
    assert abs(value_of(late, "volume_index") - 300.0) < 1e-9
    assert value_of(late, "gap") == 5.0
    assert value_of(late, "hit") == 1.0
    assert value_of(late, "vs_goal") == 112.5
    assert value_of(late, "q") == 2
    assert value_of(other, "q") == 4
    assert abs(value_of(late, "contrib") - 100.0) < 1e-9
    assert value_of(other, "contrib") == 0.0
    assert value_of(late, "smoothed") == 33.75
    assert abs(value_of(late, "on_bar") - (2.0 / 3.0) * 100) < 1e-9
    assert value_of(late, "held") == 2.0
    assert value_of(late, "seasonal") == 3.0
    for key in measures:
        assert key in late

    lead_spec = _addon_kpi(9911)
    lead_spec["measures"]["next_month"] = {
        "op": "lead",
        "of": "current_value",
        "offset": {"months": 1},
    }
    write_yaml(extra_config / "kpis" / "9911.yaml", lead_spec)
    ahead = compute(
        make_context(
            parquet_path,
            measures=["next_month"],
            supplier=["ABC"],
            kpi_id=9911,
            month="2026-02",
        ),
        config_dir=extra_config,
    )
    assert value_of(find_row(ahead, cut="G", reason="LATE_SUPPLIER"), "next_month") == 45.0
