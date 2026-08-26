"""WS4 cut extensions: precedence, from_cut, versus_cut, rollup, omit_null_rows."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_only_cut_emits_one_grain(parquet_path, extra_config):
    _write(
        extra_config,
        96001,
        parameters={"only_cut": {"type": "string", "allowed": ["G", "R"]}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=96001,
        parameters={"only_cut": "G"},
    )
    result = compute(ctx, config_dir=extra_config)
    assert {row["output_cut"] for row in result["rows"]} == {"G"}


def test_emit_cuts_intersects_also_emit(parquet_path, extra_config):
    _write(
        extra_config,
        96002,
        parameters={
            "output_cut": {"type": "string", "default": "G", "allowed": ["G", "R"]},
            "emit_cuts": {"type": "list", "item": "string", "allowed": ["G", "R"]},
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=96002,
        parameters={"output_cut": "G", "emit_cuts": ["R"]},
    )
    result = compute(ctx, config_dir=extra_config)
    assert {row["output_cut"] for row in result["rows"]} == {"R"}


def test_from_cut_broadcasts_coarser_scalar(parquet_path, extra_config):
    _write(
        extra_config,
        96003,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "g_value": {
                "of": "sotif_value",
                "op": "point",
                "from_cut": "G",
                "cuts": ["R"],
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value", "g_value"],
        supplier=["ABC"],
        kpi_id=96003,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    r_na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    r_eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    assert value_of(r_na, "g_value") == value_of(g_late, "current_value")
    assert value_of(r_eu, "g_value") == value_of(g_late, "current_value")


def test_from_cut_finer_to_coarser_is_bind_error(extra_config):
    _write(
        extra_config,
        96004,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "r_on_g": {
                "of": "sotif_value",
                "op": "point",
                "from_cut": "R",
                "cuts": ["G"],
            },
        },
    )
    with pytest.raises(BindError, match="finer"):
        load_kpi(96004, extra_config)


def test_versus_cut_rejected_on_diff(extra_config):
    _write(
        extra_config,
        96005,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "yoy": {
                "op": "diff",
                "of": "current_value",
                "offset": {"years": 1},
                "versus_cut": "G",
            },
        },
    )
    with pytest.raises(BindError, match="versus_cut"):
        load_kpi(96005, extra_config)


def test_versus_cut_allowed_on_rank(extra_config):
    kpi = _write(
        extra_config,
        96006,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "reason_rank": {
                "op": "rank",
                "of": "current_value",
                "versus_cut": "G",
                "cuts": ["R"],
            },
        },
    )
    loaded = load_kpi(96006, extra_config)
    rank = {m.key: m for m in loaded.measures}["reason_rank"]
    assert rank.versus_cut == "G"
    del kpi


def test_rollup_from_child_monthly(parquet_path, extra_config):
    cuts = [
        {
            "name": "G",
            "group_by": [],
            "exclude_from_grain": ["region"],
            "ignore_filters": ["region"],
            "also_emit": ["R"],
            "rollup_from": "R",
        },
        {"name": "R", "group_by": ["region"], "ignore_filters": []},
    ]
    _write(extra_config, 96007, cuts=cuts)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=96007,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    r_na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    r_eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    assert value_of(g_late, "current_value") == pytest.approx(
        value_of(r_na, "current_value") + value_of(r_eu, "current_value")
    )


def test_omit_null_rows_keeps_zero_drops_all_null(parquet_path, extra_config):
    _write(
        extra_config,
        96008,
        omit_null_rows=True,
        base_measures={"sotif_value": {"sql": "amount", "agg": "sum"}},
        measures={
            "masked": {
                "op": "point",
                "of": "sotif_value",
                "where": {"column": "reason_code", "op": "eq", "value": "NO_SUCH"},
            }
        },
    )
    ctx = make_context(parquet_path, measures=["masked"], kpi_id=96008)
    result = compute(ctx, config_dir=extra_config)
    assert result["rows"] == []


def test_default_measures_by_cut_fills_omitted_cuts(extra_config):
    _write(
        extra_config,
        96009,
        default_measures_by_cut={"G": ["current_value"], "R": ["value_3m"]},
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
            },
        },
    )
    kpi = load_kpi(96009, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["current_value"].cuts == ("G",)
    assert by_key["value_3m"].cuts == ("R",)
