"""Complex calculation tests: arithmetic composition, /0, nested ops.

What this file provides
    add/sub/mul/div/percent/growth_pct, nested arithmetic, division by zero,
    and a single request that asks for every 3004 measure at once.

Where it is used
    pytest tests/test_complex_calculations.py.

When to use
    Add a case when a new function is registered in ops_impl.MEASURE_FNS.
"""

from kpi_engine import compute
from kpi_engine.exceptions import KPIEngineError
from tests.conftest import find_row, make_context, write_yaml


def test_arithmetic_fns(parquet_path, extra_config):
    """Catalog arithmetic: add, sub, mul, div, percent; /0 and /null → null."""
    write_yaml(extra_config / "kpis" / "9004.yaml", _arith_kpi(9004))
    ctx = make_context(
        parquet_path,
        measures=[
            "current_value",
            "previous_year_value",
            "sum_cur_py",
            "diff_cur_py",
            "prod_cur_py",
            "ratio_cur_py",
            "percent_cur_py",
            "share_of_3m",
            "yoy_times_share",
        ],
        supplier=["ABC"],
        kpi_id=9004,
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    assert g["previous_year_value"] == 15.0
    assert g["sum_cur_py"] == 60.0
    assert g["diff_cur_py"] == 30.0
    assert g["prod_cur_py"] == 675.0
    assert g["ratio_cur_py"] == 3.0
    assert g["percent_cur_py"] == 300.0
    # 45 / 90
    assert g["share_of_3m"] == 0.5
    # yoy 2.0 * share 0.5
    assert g["yoy_times_share"] == 1.0

    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert na["previous_year_value"] is None
    assert na["ratio_cur_py"] is None
    assert na["percent_cur_py"] is None
    assert na["sum_cur_py"] is None


def test_division_by_zero_is_null(tmp_path, extra_config):
    """growth_pct / div with a zero denominator must not emit inf."""
    rows = [
        {
            "event_month": "2025-03-01",
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": 0,
        },
        {
            "event_month": "2026-03-01",
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": 10,
        },
    ]
    import pandas as pd

    path = tmp_path / "zero.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(extra_config / "kpis" / "9004.yaml", _arith_kpi(9004))
    ctx = make_context(
        path,
        measures=["yoy_month", "ratio_cur_py"],
        supplier=["ABC"],
        kpi_id=9004,
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["yoy_month"] is None
    assert g["ratio_cur_py"] is None


def test_unknown_arithmetic_fn(parquet_path, extra_config):
    """Unknown fn is rejected against the registry at bind time, not eval()."""
    spec = _arith_kpi(9005)
    spec["measures"]["bad_fn"] = {
        "op": "arithmetic",
        "fn": "eval_me",
        "left": "current_value",
        "right": "current_value",
    }
    write_yaml(extra_config / "kpis" / "9005.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["bad_fn"],
        supplier=["ABC"],
        kpi_id=9005,
    )
    try:
        compute(ctx, config_dir=extra_config)
    except KPIEngineError as exc:
        assert "eval_me" in str(exc)
    else:
        raise AssertionError("expected an engine error")


def test_all_3004_measures_together(parquet_path, config_dir):
    """One request can project every published 3004 measure without changing scan rules."""
    ctx = make_context(
        parquet_path,
        measures=[
            "reason_code",
            "current_value",
            "previous_year_value",
            "value_3m",
            "value_6m",
            "value_12m",
            "yoy_month",
            "trend_12m",
        ],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    assert g["previous_year_value"] == 15.0
    assert g["value_3m"] == 90.0
    assert g["value_6m"] == 585.0
    assert g["value_12m"] == 1170.0
    assert g["yoy_month"] == 2.0
    assert len(g["trend_12m"]) == 12
    assert result["trend_axes"]["trend_12m"][0] == "2025-04-01"
    r_rows = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert all("trend_12m" not in r for r in r_rows)


def _arith_kpi(kpi_id: int) -> dict:
    """3004 plus composed arithmetic measures."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [
            {
                "name": "G",
                "group_by": ["reason_code"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["reason_code", "region"], "ignore_filters": []},
        ],
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "reason_code": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "yoy_month": {
                "op": "arithmetic",
                "fn": "growth_pct",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "sum_cur_py": {
                "op": "arithmetic",
                "fn": "add",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "diff_cur_py": {
                "op": "arithmetic",
                "fn": "sub",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "prod_cur_py": {
                "op": "arithmetic",
                "fn": "mul",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "ratio_cur_py": {
                "op": "arithmetic",
                "fn": "div",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "percent_cur_py": {
                "op": "arithmetic",
                "fn": "percent",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "share_of_3m": {
                "op": "arithmetic",
                "fn": "div",
                "left": "current_value",
                "right": "value_3m",
            },
            "yoy_times_share": {
                "op": "arithmetic",
                "fn": "mul",
                "left": "yoy_month",
                "right": "share_of_3m",
            },
        },
    }
