"""Add-on measure kinds, hooks, and measure functions."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.capabilities.ops.cut import DenseRank
from kpi_engine.contracts import OutputSpec
from kpi_engine.core.binder import load_kpi
from kpi_engine.core.loader import list_capabilities, reload_packaged, write_generated_docs
from kpi_engine.core.op_registry import get_op
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml

ADDON_OPS = {
    "ntile": "cut",
    "dense_rank": "cut",
    "row_number": "cut",
    "cumulative_share": "cut",
    "running_total": "cut",
    "contribution": "cut",
    "lag": "combo",
    "lead": "combo",
    "index": "combo",
    "vs_target": "combo",
    "threshold": "combo",
}

ADDON_HOOKS = {
    "seasonal_index",
    "ewma",
    "period_max",
    "period_min",
    "period_median",
    "period_avg",
    "period_sum",
    "hit_rate",
    "streak",
}


def test_addon_ops_and_hooks_boot_from_packaged_yaml():
    reload_packaged()
    rows = list_capabilities()
    ops = {row["name"]: row for row in rows if row["type"] == "op"}
    hooks = {row["name"]: row for row in rows if row["type"] == "hook"}
    for name, phase in ADDON_OPS.items():
        assert ops[name]["role"] == "addon"
        assert get_op(name).phase == phase
    for name in ADDON_HOOKS:
        assert hooks[name]["role"] == "addon"
        assert hooks[name]["enabled"] is True


@pytest.mark.parametrize("kind", sorted(ADDON_OPS))
def test_unknown_key_on_each_addon_kind(extra_config, kind):
    spec = minimal_kpi(9900)
    body = {"op": kind, "of": "current_value", "foo": 1}
    if kind == "ntile":
        body["tiles"] = 4
    if kind == "contribution":
        body["vs"] = "current_value"
    if kind in {"lag", "lead", "index"}:
        body["offset"] = {"months": 1}
    if kind == "vs_target":
        body["value"] = 1
    if kind == "threshold":
        body["value"] = 1
    spec["measures"]["trial"] = body
    write_yaml(extra_config / "kpis" / "9900.yaml", spec)
    with pytest.raises(BindError, match="does not accept 'foo'"):
        load_kpi(9900, extra_config)


def test_lag_of_fn_measure_is_bind_error(extra_config):
    spec = minimal_kpi(9901)
    spec["measures"]["ratio"] = {
        "op": "fn",
        "fn": "divide",
        "inputs": ["current_value", "value_3m"],
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "ratio", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9901.yaml", spec)
    with pytest.raises(BindError, match="point, or window"):
        load_kpi(9901, extra_config)


def test_ntile_tiles_must_be_int_at_least_two(extra_config):
    spec = minimal_kpi(9902)
    spec["measures"]["q"] = {"op": "ntile", "of": "current_value", "tiles": 2.5}
    write_yaml(extra_config / "kpis" / "9902.yaml", spec)
    with pytest.raises(BindError, match="tiles"):
        load_kpi(9902, extra_config)
    spec["measures"]["q"] = {"op": "ntile", "of": "current_value"}
    write_yaml(extra_config / "kpis" / "9902.yaml", spec)
    with pytest.raises(BindError, match="tiles"):
        load_kpi(9902, extra_config)


def test_dense_rank_ties_do_not_skip():
    spec = OutputSpec(key="dr", kind="dense_rank", of="current_value", rank_order="desc")
    rows = [
        {"output_cut": "G", "reason_code": "A", "__cut_src_dr": 10},
        {"output_cut": "G", "reason_code": "B", "__cut_src_dr": 10},
        {"output_cut": "G", "reason_code": "C", "__cut_src_dr": 5},
    ]
    DenseRank().apply_to_cut(rows, spec, ["reason_code"])
    assert [row["dr"] for row in rows] == [1, 1, 2]


def test_cut_addons_compute(parquet_path, extra_config):
    spec = minimal_kpi(9903)
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"years": 1},
    }
    spec["measures"]["q"] = {"op": "ntile", "of": "current_value", "tiles": 4, "order": "desc"}
    spec["measures"]["dr"] = {"op": "dense_rank", "of": "current_value", "order": "desc"}
    spec["measures"]["rn"] = {"op": "row_number", "of": "current_value", "order": "desc"}
    spec["measures"]["pareto"] = {"op": "cumulative_share", "of": "current_value", "order": "desc"}
    spec["measures"]["running"] = {"op": "running_total", "of": "current_value", "order": "desc"}
    spec["measures"]["contrib"] = {
        "op": "contribution",
        "of": "current_value",
        "vs": "previous_year_value",
    }
    write_yaml(extra_config / "kpis" / "9903.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "q", "dr", "rn", "pareto", "running", "contrib"],
        supplier=["ABC"],
        kpi_id=9903,
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")
    assert late["current_value"] == 45.0
    assert other["current_value"] == 6.0
    assert late["q"] == 2
    assert other["q"] == 4
    assert late["dr"] == 1
    assert other["dr"] == 2
    assert {late["rn"], other["rn"]} == {1, 2}
    assert abs(late["pareto"] - (45.0 / 51.0) * 100) < 1e-9
    assert abs(other["pareto"] - 100.0) < 1e-9
    assert late["running"] == 45.0
    assert other["running"] == 51.0
    assert abs(late["contrib"] - 100.0) < 1e-9
    assert other["contrib"] == 0.0


def test_lag_lead_index_vs_target_threshold(parquet_path, extra_config):
    spec = minimal_kpi(9904)
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
    spec["measures"]["gap"] = {"op": "vs_target", "of": "current_value", "vs": "target", "as": "gap"}
    spec["measures"]["gap_pct"] = {
        "op": "vs_target",
        "of": "current_value",
        "vs": "target",
        "as": "pct",
    }
    spec["measures"]["hit"] = {"op": "threshold", "of": "current_value", "cmp": "gte", "value": 95}
    write_yaml(extra_config / "kpis" / "9904.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m", "value_3m_ly", "volume_index", "gap", "gap_pct", "hit"],
        supplier=["ABC"],
        kpi_id=9904,
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert late["value_3m"] == 90.0
    assert late["value_3m_ly"] == 60.0
    assert abs(late["volume_index"] - 300.0) < 1e-9
    assert late["gap"] == 5.0
    assert abs(late["gap_pct"] - 12.5) < 1e-9
    assert late["hit"] == 0.0

    spec["measures"]["next_month"] = {
        "op": "lead",
        "of": "current_value",
        "offset": {"months": 1},
    }
    write_yaml(extra_config / "kpis" / "9904.yaml", spec)
    ahead = compute(
        make_context(
            parquet_path,
            measures=["next_month"],
            supplier=["ABC"],
            kpi_id=9904,
            month="2026-02",
        ),
        config_dir=extra_config,
    )
    assert find_row(ahead, cut="G", reason="LATE_SUPPLIER")["next_month"] == 45.0


def test_measure_fns_abs_clamp_attainment(parquet_path, extra_config):
    spec = minimal_kpi(9905)
    spec["measures"]["target"] = {"op": "constant", "value": 50}
    spec["measures"]["floor"] = {"op": "constant", "value": 0}
    spec["measures"]["cap"] = {"op": "constant", "value": 40}
    spec["measures"]["gap"] = {
        "op": "fn",
        "fn": "subtract",
        "inputs": ["current_value", "target"],
    }
    spec["measures"]["magnitude"] = {"op": "fn", "fn": "abs", "inputs": ["gap"]}
    spec["measures"]["bounded"] = {
        "op": "fn",
        "fn": "clamp",
        "inputs": ["current_value", "floor", "cap"],
    }
    spec["measures"]["vs_goal"] = {
        "op": "fn",
        "fn": "attainment",
        "inputs": ["current_value", "target"],
    }
    write_yaml(extra_config / "kpis" / "9905.yaml", spec)
    result = compute(
        make_context(
            parquet_path,
            measures=["magnitude", "bounded", "vs_goal"],
            supplier=["ABC"],
            kpi_id=9905,
        ),
        config_dir=extra_config,
    )
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert late["magnitude"] == 5.0
    assert late["bounded"] == 40.0
    assert late["vs_goal"] == 90.0


def test_hooks_series_formulas(parquet_path, extra_config):
    spec = minimal_kpi(9906)
    spec["measures"]["seasonal"] = {
        "op": "hook",
        "hook": "seasonal_index",
        "of": "sotif_value",
        "trailing": {"months": 15},
    }
    spec["measures"]["smoothed"] = {
        "op": "hook",
        "hook": "ewma",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["best"] = {
        "op": "hook",
        "hook": "period_max",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["typical"] = {
        "op": "hook",
        "hook": "period_median",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["mean"] = {
        "op": "hook",
        "hook": "period_avg",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["total"] = {
        "op": "hook",
        "hook": "period_sum",
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
    write_yaml(extra_config / "kpis" / "9906.yaml", spec)
    result = compute(
        make_context(
            parquet_path,
            measures=[
                "current_value",
                "seasonal",
                "smoothed",
                "best",
                "typical",
                "mean",
                "total",
                "on_bar",
                "held",
            ],
            supplier=["ABC"],
            kpi_id=9906,
        ),
        config_dir=extra_config,
    )
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert late["current_value"] == 45.0
    assert late["seasonal"] == 3.0
    assert late["smoothed"] == 33.75
    assert late["best"] == 45.0
    assert late["typical"] == 30.0
    assert late["mean"] == 30.0
    assert late["total"] == 90.0
    assert abs(late["on_bar"] - (2.0 / 3.0) * 100) < 1e-9
    assert late["held"] == 2.0


def test_hit_rate_requires_value(extra_config):
    spec = minimal_kpi(9907)
    spec["measures"]["on_bar"] = {
        "op": "hook",
        "hook": "hit_rate",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    write_yaml(extra_config / "kpis" / "9907.yaml", spec)
    with pytest.raises(BindError, match="value"):
        load_kpi(9907, extra_config)


def test_generated_catalog_includes_addons():
    text = write_generated_docs().read_text()
    assert "`ntile`" in text
    assert "`lag`" in text
    assert "`ewma`" in text
    assert "`period_avg`" in text
    assert "`period_sum`" in text
    assert "role: addon" in text
