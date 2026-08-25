"""Add-on measure kinds, hooks, and measure functions."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.capabilities.ops.cut import DenseRank
from kpi_engine.contracts import OutputSpec
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.loader import list_capabilities, reload_packaged, write_generated_docs
from kpi_engine.pipeline.op_registry import get_op
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _addon_rows(kind: str) -> list[dict]:
    reload_packaged()
    return [
        row
        for row in list_capabilities()
        if row["role"] == "addon" and row["type"] == kind
    ]


def test_addon_ops_and_hooks_boot_from_packaged_yaml():
    from kpi_engine.pipeline.loader import skipped_addons
    from kpi_engine.pipeline.hook_registry import REGISTRY

    ops = _addon_rows("op")
    hooks = _addon_rows("hook")
    assert ops and hooks
    skipped = skipped_addons()
    for row in ops:
        assert row["name"] not in skipped
        assert get_op(row["name"]).phase in {"cut", "combo"}
        assert row["enabled"] is True
    for row in hooks:
        assert row["name"] not in skipped
        assert row["name"] in REGISTRY
        assert row["enabled"] is True
    by_name = {row["name"]: row for row in hooks}
    assert by_name["hit_rate"]["requires_value"] is True
    assert by_name["ewma"]["requires_value"] is False


@pytest.mark.parametrize("kind", [row["name"] for row in _addon_rows("op")])
def test_unknown_key_on_each_addon_kind(extra_config, kind):
    spec = minimal_kpi(9900)
    spec["measures"]["trial"] = {"op": kind, "of": "current_value", "foo": 1}
    write_yaml(extra_config / "kpis" / "9900.yaml", spec)
    with pytest.raises(BindError, match="does not accept 'foo'"):
        load_kpi(9900, extra_config)


@pytest.mark.parametrize("row", _addon_rows("hook"), ids=lambda row: row["name"])
def test_unknown_key_on_each_addon_hook(extra_config, row):
    spec = minimal_kpi(9900)
    body = {"op": "hook", "hook": row["name"], "of": "sotif_value", "foo": 1}
    if row["requires_value"]:
        body["value"] = 1
    spec["measures"]["trial"] = body
    write_yaml(extra_config / "kpis" / "9900.yaml", spec)
    with pytest.raises(BindError, match="does not accept 'foo'"):
        load_kpi(9900, extra_config)


def test_lag_of_fn_measure_matches_prior_month_ratio(parquet_path, extra_config):
    """lag of a fn composite is last month's ratio, not this month's (F1)."""
    spec = minimal_kpi(9901)
    spec["measures"]["ratio"] = {
        "op": "fn",
        "fn": "divide",
        "inputs": ["current_value", "value_3m"],
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "ratio", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9901.yaml", spec)
    load_kpi(9901, extra_config)

    march = compute(
        make_context(
            parquet_path,
            measures=["ratio", "lagged"],
            supplier=["ABC"],
            kpi_id=9901,
            month="2026-03",
        ),
        config_dir=extra_config,
    )
    feb = compute(
        make_context(
            parquet_path,
            measures=["ratio"],
            supplier=["ABC"],
            kpi_id=9901,
            month="2026-02",
        ),
        config_dir=extra_config,
    )
    march_row = find_row(march, cut="R", reason="LATE_SUPPLIER", region="NA")
    feb_row = find_row(feb, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert march_row["lagged"] == pytest.approx(feb_row["ratio"])
    assert march_row["lagged"] != pytest.approx(march_row["ratio"])


def test_lag_of_point_offset_equals_double_offset(parquet_path, extra_config):
    """lag of (point offset 1) equals point offset 2 on the same base."""
    spec = minimal_kpi(9910)
    spec["measures"]["one_back"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"months": 1},
    }
    spec["measures"]["lagged_one"] = {"op": "lag", "of": "one_back", "offset": {"months": 1}}
    spec["measures"]["two_back"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"months": 2},
    }
    write_yaml(extra_config / "kpis" / "9910.yaml", spec)
    row = find_row(
        compute(
            make_context(
                parquet_path,
                measures=["lagged_one", "two_back"],
                supplier=["ABC"],
                kpi_id=9910,
            ),
            config_dir=extra_config,
        ),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["lagged_one"] == pytest.approx(row["two_back"])


def test_lag_of_helper_is_bind_error(extra_config):
    spec = minimal_kpi(9911)
    spec["base_measures"]["rate"] = {
        "lookup": {"column": "region", "map": {"NA": 1.0}, "default": 0},
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "rate", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9911.yaml", spec)
    with pytest.raises(BindError, match="row helper"):
        load_kpi(9911, extra_config)


def test_lag_of_fn_of_hook_is_shiftable(extra_config):
    spec = minimal_kpi(9915)
    spec["measures"]["smoothed"] = {
        "op": "hook",
        "hook": "ewma",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["scaled"] = {
        "op": "fn",
        "fn": "multiply",
        "inputs": ["smoothed", "current_value"],
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "scaled", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9915.yaml", spec)
    kpi = load_kpi(9915, extra_config)
    assert {m.key for m in kpi.measures} >= {"smoothed", "scaled", "lagged"}


def test_lag_of_rank_is_bind_error(extra_config):
    spec = minimal_kpi(9913)
    spec["measures"]["reason_rank"] = {
        "op": "rank",
        "of": "current_value",
        "order": "desc",
        "cuts": ["G"],
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "reason_rank", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9913.yaml", spec)
    with pytest.raises(BindError, match="op=rank"):
        load_kpi(9913, extra_config)


def test_lag_of_ratio_on_missing_prior_month_is_not_current(parquet_path, extra_config):
    """NA LATE_SUPPLIER has no 2025-03 row; lag at 2025-04 must not copy April."""
    spec = minimal_kpi(9914)
    spec["measures"]["ratio"] = {
        "op": "fn",
        "fn": "divide",
        "inputs": ["current_value", "value_3m"],
    }
    spec["measures"]["lagged"] = {"op": "lag", "of": "ratio", "offset": {"months": 1}}
    write_yaml(extra_config / "kpis" / "9914.yaml", spec)
    row = find_row(
        compute(
            make_context(
                parquet_path,
                measures=["ratio", "lagged"],
                supplier=["ABC"],
                kpi_id=9914,
                month="2025-04",
            ),
            config_dir=extra_config,
        ),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["ratio"] is not None
    assert row["lagged"] != pytest.approx(row["ratio"])


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


def test_recommended_cut_ops_compute(parquet_path, extra_config):
    spec = minimal_kpi(9912)
    spec["measures"]["pct_rank"] = {
        "op": "percent_rank",
        "of": "current_value",
        "order": "desc",
    }
    spec["measures"]["vs_best"] = {"op": "gap_to_leader", "of": "current_value"}
    spec["measures"]["vs_mean"] = {"op": "gap_to_avg", "of": "current_value"}
    spec["measures"]["z"] = {"op": "zscore", "of": "current_value"}
    spec["measures"]["run_avg"] = {
        "op": "running_avg",
        "of": "current_value",
        "order": "desc",
    }
    spec["measures"]["top"] = {"op": "top_n", "of": "current_value", "n": 1, "order": "desc"}
    write_yaml(extra_config / "kpis" / "9912.yaml", spec)
    result = compute(
        make_context(
            parquet_path,
            measures=["current_value", "pct_rank", "vs_best", "vs_mean", "z", "run_avg", "top"],
            supplier=["ABC"],
            kpi_id=9912,
        ),
        config_dir=extra_config,
    )
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")
    assert late["pct_rank"] == 0.0
    assert other["pct_rank"] == 100.0
    assert late["vs_best"] == 0.0
    assert other["vs_best"] == -39.0
    assert late["vs_mean"] == 19.5
    assert other["vs_mean"] == -19.5
    stdev = 760.5 ** 0.5
    assert abs(late["z"] - (19.5 / stdev)) < 1e-9
    assert abs(other["z"] - (-19.5 / stdev)) < 1e-9
    assert late["run_avg"] == 45.0
    assert other["run_avg"] == 25.5
    assert late["top"] == 1.0
    assert other["top"] == 0.0


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
    spec["measures"]["yoy_gap"] = {"op": "diff", "of": "current_value", "offset": {"years": 1}}
    spec["measures"]["yoy_pct"] = {
        "op": "pct_change",
        "of": "current_value",
        "offset": {"years": 1},
    }
    write_yaml(extra_config / "kpis" / "9904.yaml", spec)
    shifted = compute(
        make_context(
            parquet_path,
            measures=["yoy_gap", "yoy_pct"],
            supplier=["ABC"],
            kpi_id=9904,
        ),
        config_dir=extra_config,
    )
    late_shift = find_row(shifted, cut="G", reason="LATE_SUPPLIER")
    assert late_shift["yoy_gap"] == 30.0
    assert abs(late_shift["yoy_pct"] - 2.0) < 1e-9

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
    spec["measures"]["vol"] = {
        "op": "hook",
        "hook": "period_stdev",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["variance"] = {
        "op": "hook",
        "hook": "period_var",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["rel_vol"] = {
        "op": "hook",
        "hook": "period_cv",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["spread"] = {
        "op": "hook",
        "hook": "period_range",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["seen"] = {
        "op": "hook",
        "hook": "period_count",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["off_bar"] = {
        "op": "hook",
        "hook": "miss_rate",
        "of": "sotif_value",
        "trailing": {"months": 3},
        "value": 30,
    }
    spec["measures"]["off_run"] = {
        "op": "hook",
        "hook": "miss_streak",
        "of": "sotif_value",
        "trailing": {"months": 3},
        "value": 30,
    }
    spec["measures"]["best_run"] = {
        "op": "hook",
        "hook": "longest_streak",
        "of": "sotif_value",
        "trailing": {"months": 3},
        "value": 30,
    }
    spec["measures"]["annualized"] = {
        "op": "hook",
        "hook": "cagr",
        "of": "sotif_value",
        "trailing": {"months": 3},
    }
    spec["measures"]["tilt"] = {
        "op": "hook",
        "hook": "slope",
        "of": "sotif_value",
        "trailing": {"months": 3},
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
                "vol",
                "variance",
                "rel_vol",
                "spread",
                "seen",
                "off_bar",
                "off_run",
                "best_run",
                "annualized",
                "tilt",
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
    assert late["vol"] == 15.0
    assert late["variance"] == 225.0
    assert late["rel_vol"] == 50.0
    assert late["spread"] == 30.0
    assert late["seen"] == 3.0
    assert abs(late["off_bar"] - (1.0 / 3.0) * 100) < 1e-9
    assert late["off_run"] == 0.0
    assert late["best_run"] == 2.0
    assert abs(late["annualized"] - (3.0 ** 6 - 1.0)) < 1e-9
    assert late["tilt"] == 15.0


@pytest.mark.parametrize(
    "hook",
    [row["name"] for row in _addon_rows("hook") if row["requires_value"]],
)
def test_bar_hooks_require_value(extra_config, hook):
    spec = minimal_kpi(9907)
    spec["measures"]["on_bar"] = {
        "op": "hook",
        "hook": hook,
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
    assert "`percent_rank`" in text
    assert "`diff`" in text
    assert "`cagr`" in text
    assert "role: addon" in text
