"""op: constant and op: rank.

What this file provides
    A literal target used in arithmetic, and RANK() across a cut (ties skip).

Where it is used
    pytest tests/test_constant_and_rank.py.

When to use
    Add a case when rank partitioning or constant typing changes.
"""

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_constant_used_as_percent_target(parquet_path, extra_config):
    """target: 0.98 is the same on every row and can sit on the right of percent."""
    spec = minimal_kpi(
        9840,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "target": {"op": "constant", "value": 0.98},
            "percent_gt": {
                "op": "arithmetic",
                "fn": "percent",
                "left": "current_value",
                "right": "target",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9840.yaml", spec)
    kpi = load_kpi(9840, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["target"].kind == "constant"
    assert by_key["target"].constant == 0.98
    ctx = make_context(
        parquet_path,
        measures=["current_value", "target", "percent_gt"],
        supplier=["ABC"],
        kpi_id=9840,
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["target"] == 0.98
    assert row["current_value"] == 30.0
    assert abs(row["percent_gt"] - (30.0 / 0.98) * 100) < 1e-9


def test_constant_requires_a_number(extra_config):
    """op: constant without value: fails at bind."""
    spec = minimal_kpi(9841, measures={"target": {"op": "constant"}})
    write_yaml(extra_config / "kpis" / "9841.yaml", spec)
    try:
        load_kpi(9841, extra_config)
    except BindError as exc:
        assert "constant" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_rank_desc_across_reason_codes_on_cut_g(parquet_path, extra_config):
    """At G, rank of current_value desc: OTHER (6) is 2, LATE (45) is 1."""
    spec = minimal_kpi(9842)
    spec["measures"]["reason_code_rank"] = {
        "op": "rank",
        "of": "current_value",
        "group_by": ["Reason_Code"],
        "order": "desc",
        "cuts": ["G"],
    }
    write_yaml(extra_config / "kpis" / "9842.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "reason_code_rank"],
        supplier=["ABC"],
        kpi_id=9842,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    assert g_late["current_value"] == 45.0
    assert g_other["current_value"] == 6.0
    assert g_late["reason_code_rank"] == 1
    assert g_other["reason_code_rank"] == 2
    r_rows = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert all("reason_code_rank" not in r for r in r_rows)


def test_rank_of_base_measure_at_anchor(parquet_path, extra_config):
    """of: sotif_value is the anchor point of that base fact."""
    spec = minimal_kpi(9843)
    spec["measures"]["reason_code_rank"] = {
        "op": "rank",
        "of": "sotif_value",
        "order": "desc",
        "cuts": ["G"],
    }
    write_yaml(extra_config / "kpis" / "9843.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["reason_code_rank"],
        supplier=["ABC"],
        kpi_id=9843,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    assert g_late["reason_code_rank"] == 1
    assert g_other["reason_code_rank"] == 2
