"""op: percent_of_total — share of all groups on a cut (SUM() OVER ()).

What this file provides
    Whole-cut share, partition_by, zero total, cuts: restriction, bind errors.

Where it is used
    pytest tests/test_percent_of_total.py.

When to use
    Add a case when cut-wide share partitioning or null/zero totals change.
"""

from kpi_engine import compute
from kpi_engine.contracts import OutputSpec
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.capabilities.ops.cut import PercentOfTotal
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def test_percent_of_total_across_reason_codes_on_cut_g(parquet_path, extra_config):
    """At G, LATE is 45/51*100 and OTHER is 6/51*100; omitted on cut R."""
    spec = minimal_kpi(9850)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "cuts": ["G"],
    }
    write_yaml(extra_config / "kpis" / "9850.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "percent_gt"],
        supplier=["ABC"],
        kpi_id=9850,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    assert value_of(g_late, "current_value") == 45.0
    assert value_of(g_other, "current_value") == 6.0
    assert abs(value_of(g_late, "percent_gt") - (45.0 / 51.0) * 100) < 1e-9
    assert abs(value_of(g_other, "percent_gt") - (6.0 / 51.0) * 100) < 1e-9
    r_rows = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert all("percent_gt" not in r for r in r_rows)


def test_percent_of_total_of_base_measure(parquet_path, extra_config):
    """of: sotif_value is the anchor point of that base fact."""
    spec = minimal_kpi(9851)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "sotif_value",
        "cuts": ["G"],
    }
    write_yaml(extra_config / "kpis" / "9851.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["percent_gt"],
        supplier=["ABC"],
        kpi_id=9851,
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    assert abs(value_of(g_late, "percent_gt") - (45.0 / 51.0) * 100) < 1e-9
    assert abs(value_of(g_other, "percent_gt") - (6.0 / 51.0) * 100) < 1e-9


def test_percent_of_total_partition_by_reason_on_cut_r(parquet_path, extra_config):
    """On R, partition_by reason_code shares NA/EU within LATE (30/45 and 15/45)."""
    spec = minimal_kpi(9852)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "partition_by": ["reason_code"],
        "cuts": ["R"],
    }
    write_yaml(extra_config / "kpis" / "9852.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "percent_gt"],
        supplier=["ABC"],
        kpi_id=9852,
    )
    result = compute(ctx, config_dir=extra_config)
    na_late = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu_late = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    na_other = find_row(result, cut="R", reason="OTHER", region="NA")
    assert value_of(na_late, "current_value") == 30.0
    assert value_of(eu_late, "current_value") == 15.0
    assert abs(value_of(na_late, "percent_gt") - (30.0 / 45.0) * 100) < 1e-9
    assert abs(value_of(eu_late, "percent_gt") - (15.0 / 45.0) * 100) < 1e-9
    assert abs(value_of(na_other, "percent_gt") - 100.0) < 1e-9


def test_percent_of_total_zero_total_is_null():
    """A partition whose sources sum to 0 yields null, not inf."""
    spec = OutputSpec(key="percent_gt", kind="percent_of_total", of="current_value")
    rows = [
        {"output_cut": "G", "reason_code": "A", "__cut_src_percent_gt": 0},
        {"output_cut": "G", "reason_code": "B", "__cut_src_percent_gt": 0},
    ]
    PercentOfTotal().apply_to_cut(rows, spec, ["reason_code"])
    assert rows[0]["percent_gt"] is None
    assert rows[1]["percent_gt"] is None
    assert "__cut_src_percent_gt" not in rows[0]


def test_percent_of_total_requires_of(extra_config):
    """Missing of: fails at bind and lists declared measures."""
    spec = minimal_kpi(9853, measures={"percent_gt": {"op": "percent_of_total"}})
    write_yaml(extra_config / "kpis" / "9853.yaml", spec)
    try:
        load_kpi(9853, extra_config)
    except BindError as exc:
        assert "percent_of_total" in str(exc)
        assert "of:" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_percent_of_cut_total_hint(extra_config):
    """A hook-style name points authors at op: percent_of_total."""
    spec = minimal_kpi(
        9854,
        measures={"percent_gt": {"op": "percent_of_cut_total", "of": "current_value"}},
    )
    write_yaml(extra_config / "kpis" / "9854.yaml", spec)
    try:
        load_kpi(9854, extra_config)
    except BindError as exc:
        assert "percent_of_total" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_percent_of_total_feeds_arithmetic(extra_config):
    """percent_of_total can feed arithmetic/fn/expr via the cut-derived pass."""
    spec = minimal_kpi(9856)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "cuts": ["G"],
    }
    spec["measures"]["scaled"] = {
        "op": "arithmetic",
        "fn": "multiply",
        "left": "percent_gt",
        "right": "current_value",
    }
    write_yaml(extra_config / "kpis" / "9856.yaml", spec)
    kpi = load_kpi(9856, extra_config)
    scaled = next(m for m in kpi.measures if m.key == "scaled")
    assert scaled.cut_derived is True


def test_percent_of_total_unknown_partition_by(extra_config):
    """partition_by must name a dimension or cut group_by."""
    spec = minimal_kpi(9855)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "partition_by": ["plant"],
        "cuts": ["G"],
    }
    write_yaml(extra_config / "kpis" / "9855.yaml", spec)
    try:
        load_kpi(9855, extra_config)
    except BindError as exc:
        assert "partition_by" in str(exc)
        assert "plant" in str(exc)
    else:
        raise AssertionError("expected BindError")
