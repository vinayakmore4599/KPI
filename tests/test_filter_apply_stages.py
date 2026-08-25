"""KPI YAML filters: extract vs calc vs result, and every comparison op.

What this file provides
    Same OTHER reason filter at extract/calc (share = 100) vs result (share
    stays 45/51). One case per comparison op at extract and at calc.
    Bind errors for unknown op, arity, result+ignore_filters, optional skip.

Where it is used
    pytest tests/test_filter_apply_stages.py.

When to use
    Add a case when a new filter op or apply stage is added.
"""

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.filter_ops import pandas_mask, sql_predicate
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _share_kpi(kpi_id: int, apply: str, op: str = "in", column: str = "reason_code", **extra):
    """minimal_kpi plus percent_gt and one declared filter."""
    spec = minimal_kpi(kpi_id)
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "cuts": ["G"],
    }
    spec["filters"] = {
        "reason_code": {
            "column": column,
            "op": op,
            "apply": apply,
            **extra,
        }
    }
    return spec


def test_other_filter_extract_and_calc_match_on_percent_of_total(parquet_path, extra_config):
    """Filtering OTHER before measures leaves LATE at 100% of the remaining total."""
    for apply, kpi_id in (("extract", 9860), ("calc", 9861)):
        spec = _share_kpi(kpi_id, apply)
        write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
        ctx = make_context(
            parquet_path,
            measures=["current_value", "percent_gt"],
            supplier=["ABC"],
            kpi_id=kpi_id,
            extra_filters={"reason_code": {"values": ["LATE_SUPPLIER"], "input_text": "simple"}},
        )
        result = compute(ctx, config_dir=extra_config)
        late = find_row(result, cut="G", reason="LATE_SUPPLIER")
        assert late["current_value"] == 45.0
        assert abs(late["percent_gt"] - 100.0) < 1e-9
        assert all(r["reason_code"] != "OTHER" for r in result["rows"])
        planned = validate(ctx, config_dir=extra_config)
        sql = " ".join(planned["sql"].split())
        if apply == "extract":
            assert '"reason_code" IN (?)' in sql
        else:
            assert '"reason_code" IN' not in sql


def test_other_filter_at_result_keeps_unfiltered_share(parquet_path, extra_config):
    """result hides OTHER after calc; LATE still reports 45/51 of the full cut."""
    spec = _share_kpi(9862, "result")
    write_yaml(extra_config / "kpis" / "9862.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "percent_gt"],
        supplier=["ABC"],
        kpi_id=9862,
        extra_filters={"reason_code": {"values": ["LATE_SUPPLIER"], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert late["current_value"] == 45.0
    assert abs(late["percent_gt"] - (45.0 / 51.0) * 100) < 1e-9
    assert all(r["reason_code"] != "OTHER" for r in result["rows"])
    planned = validate(ctx, config_dir=extra_config)
    assert '"reason_code" IN' not in " ".join(planned["sql"].split())
    applied = {f["filter_code"]: f for f in result["applied_filters"]}
    assert applied["reason_code"]["stage"] == "result"
    assert applied["reason_code"]["apply"] == "result"
    assert applied["reason_code"]["op"] == "in"


# (canonical, yaml op alias, context values, column, expected G LATE current_value, sql needle)
_OP_CASES = [
    ("in", "IN", ["LATE_SUPPLIER"], "reason_code", 45.0, '"reason_code" IN (?)'),
    ("eq", "==", ["LATE_SUPPLIER"], "reason_code", 45.0, '"reason_code" = ?'),
    ("ne", "<>", ["OTHER"], "reason_code", 45.0, '"reason_code" <> ?'),
    ("lt", "<", [20], "amount", 15.0, '"amount" < ?'),
    ("lte", "<=", [15], "amount", 15.0, '"amount" <= ?'),
    ("gt", ">", [20], "amount", 30.0, '"amount" > ?'),
    ("gte", ">=", [30], "amount", 30.0, '"amount" >= ?'),
    ("like", "LIKE", ["%LATE%"], "reason_code", 45.0, '"reason_code" LIKE ?'),
    ("ilike", "ILIKE", ["%late%"], "reason_code", 45.0, '"reason_code" ILIKE ?'),
    ("not_like", "NOT LIKE", ["%OTHER%"], "reason_code", 45.0, '"reason_code" NOT LIKE ?'),
    ("between", "BETWEEN", [10, 20], "amount", 15.0, '"amount" BETWEEN ? AND ?'),
    ("not_between", "NOT BETWEEN", [10, 20], "amount", 30.0, '"amount" NOT BETWEEN ? AND ?'),
    ("is_null", "IS NULL", [], "reason_code", None, '"reason_code" IS NULL'),
    ("is_not_null", "IS NOT NULL", [], "reason_code", 45.0, '"reason_code" IS NOT NULL'),
]


@pytest.mark.parametrize("apply", ["extract", "calc"])
@pytest.mark.parametrize("canonical, yaml_op, values, column, late, sql_needle", _OP_CASES)
def test_each_comparison_op_at_extract_and_calc(
    parquet_path,
    extra_config,
    apply,
    canonical,
    yaml_op,
    values,
    column,
    late,
    sql_needle,
):
    """Every YAML op compiles at extract and masks the same way at calc."""
    kpi_id = 9870
    spec = minimal_kpi(kpi_id)
    spec["filters"] = {
        "op_filter": {"column": column, "op": yaml_op, "apply": apply},
    }
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    extra = {"op_filter": {"values": values, "input_text": "simple"}}
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=kpi_id,
        extra_filters=extra,
    )
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    if apply == "extract":
        assert sql_needle in sql
    else:
        assert sql_needle not in sql
    result = compute(ctx, config_dir=extra_config)
    applied = {f["filter_code"]: f for f in result["applied_filters"]}
    assert applied["op_filter"]["op"] == canonical
    assert applied["op_filter"]["stage"] == apply
    if late is None:
        assert all(r.get("reason_code") != "LATE_SUPPLIER" for r in result["rows"])
        return
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g_late["current_value"] == late


def test_calc_region_with_ignore_filters_keeps_g_worldwide(parquet_path, extra_config):
    """apply: calc + cuts.G.ignore_filters: [region] — G is worldwide, R is NA."""
    spec = minimal_kpi(9886)
    spec["filters"] = {"region": {"column": "region", "op": "in", "apply": "calc"}}
    write_yaml(extra_config / "kpis" / "9886.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9886,
    )
    planned = validate(ctx, config_dir=extra_config)
    assert '"region" IN' not in " ".join(planned["sql"].split())
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    assert find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")["current_value"] == 30.0
    assert {r["region"] for r in result["rows"] if r["output_cut"] == "R"} == {"NA"}


def test_optional_filter_skipped_when_omitted_or_null(parquet_path, extra_config):
    """optional: true + missing/null EffectiveDay-style value does not shrink the extract."""
    spec = minimal_kpi(9880)
    spec["filters"] = {
        "effective_day": {
            "column": "amount",
            "op": "lte",
            "optional": True,
            "apply": "extract",
        }
    }
    write_yaml(extra_config / "kpis" / "9880.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9880
    )
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    assert all(f["filter_code"] != "effective_day" for f in result["applied_filters"])

    ctx["filters"]["effective_day"] = {"values": [None], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0

    ctx["filters"]["effective_day"] = {"values": [15], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 15.0


def test_declared_filter_omitted_from_context_does_not_shrink_extract(
    parquet_path, extra_config
):
    """YAML-declared filters are never required; omit the key to skip."""
    spec = minimal_kpi(9881)
    spec["filters"] = {"reason_code": {"column": "reason_code", "op": "in"}}
    write_yaml(extra_config / "kpis" / "9881.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9881
    )
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    assert all(f["filter_code"] != "reason_code" for f in result["applied_filters"])
    assert result["skipped_filters"] == []


def test_optional_false_is_a_bind_error(extra_config):
    """optional: false is rejected; row filters cannot be required."""
    spec = minimal_kpi(9887)
    spec["filters"] = {
        "reason_code": {"column": "reason_code", "op": "in", "optional": False}
    }
    write_yaml(extra_config / "kpis" / "9887.yaml", spec)
    from kpi_engine.pipeline.binder import load_kpi

    with pytest.raises(BindError, match="optional: false is not supported"):
        load_kpi(9887, extra_config)


def test_extract_filter_in_ignore_filters_loads_and_defers(parquet_path, extra_config):
    """apply: extract + ignore_filters: [Region] loads; G+R keeps region out of SQL."""
    spec = minimal_kpi(9882)
    spec["filters"] = {"region": {"column": "region", "op": "in", "apply": "extract"}}
    write_yaml(extra_config / "kpis" / "9882.yaml", spec)
    from kpi_engine.pipeline.binder import load_kpi

    load_kpi(9882, extra_config)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9882,
    )
    planned = validate(ctx, config_dir=extra_config)
    assert '"region" IN' not in " ".join(planned["sql"].split())
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    assert find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")["current_value"] == 30.0
    assert {r["region"] for r in result["rows"] if r["output_cut"] == "R"} == {"NA"}


def test_result_filter_cannot_be_in_ignore_filters(extra_config):
    """ignore_filters is only for calc; result is not a fourth apply."""
    spec = minimal_kpi(9883)
    spec["filters"] = {"region": {"column": "region", "op": "in", "apply": "result"}}
    write_yaml(extra_config / "kpis" / "9883.yaml", spec)
    from kpi_engine.pipeline.binder import load_kpi

    with pytest.raises(BindError, match="apply: result cannot be listed in ignore_filters"):
        load_kpi(9883, extra_config)


def test_unknown_op_and_wrong_arity_are_bind_errors(parquet_path, extra_config):
    """Unknown op lists the table; BETWEEN with one value names the expected count."""
    spec = minimal_kpi(9884)
    spec["filters"] = {"reason_code": {"column": "reason_code", "op": "nearby"}}
    write_yaml(extra_config / "kpis" / "9884.yaml", spec)
    from kpi_engine.pipeline.binder import load_kpi

    with pytest.raises(BindError, match="Unknown filter op 'nearby'"):
        load_kpi(9884, extra_config)

    spec = minimal_kpi(9885)
    spec["filters"] = {
        "amount_band": {"column": "amount", "op": "between", "apply": "extract"}
    }
    write_yaml(extra_config / "kpis" / "9885.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9885,
        extra_filters={"amount_band": {"values": [10], "input_text": "simple"}},
    )
    with pytest.raises(BindError, match="expects 2 value"):
        validate(ctx, config_dir=extra_config)


def test_pandas_mask_matches_sql_null_semantics():
    """Nulls fail every comparison except is_null / is_not_null — same as DuckDB."""
    col = pd.Series(["LATE", None, "OTHER"])
    assert pandas_mask(col, "eq", ("LATE",)).tolist() == [True, False, False]
    assert pandas_mask(col, "ne", ("LATE",)).tolist() == [False, False, True]
    assert pandas_mask(col, "is_null", ()).tolist() == [False, True, False]
    assert pandas_mask(col, "like", ("%A%",)).tolist() == [True, False, False]
    sql, params = sql_predicate('"reason_code"', "lte", (15,))
    assert sql == '"reason_code" <= ?'
    assert params == [15]


def test_unmapped_blank_filter_is_skipped(parquet_path, config_dir):
    """Host keys sent as [] are skipped even when they have no column."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        extra_filters={"not_a_column": {"value": [], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=config_dir)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    assert {"filter_code": "not_a_column", "reason": "blank"} in result["skipped_filters"]


def test_empty_string_filter_is_not_skipped(parquet_path, config_dir):
    """[''] is a real IN value, not blank skip."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=[""],
    )
    result = compute(ctx, config_dir=config_dir)
    assert all(f["filter_code"] != "region" or f.get("reason") != "blank" for f in result["skipped_filters"])
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0
    r_rows = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert r_rows == [] or all(r.get("region") == "" for r in r_rows)


def test_is_null_omitted_skips_present_applies(parquet_path, extra_config):
    """is_null runs only when the key is on the context."""
    spec = minimal_kpi(9888)
    spec["filters"] = {
        "reason_code": {"column": "reason_code", "op": "is_null", "apply": "extract"}
    }
    write_yaml(extra_config / "kpis" / "9888.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9888
    )
    omitted = compute(ctx, config_dir=extra_config)
    assert find_row(omitted, cut="G", reason="LATE_SUPPLIER")["current_value"] == 45.0

    ctx["filters"]["reason_code"] = {"values": [], "input_text": "simple"}
    present = compute(ctx, config_dir=extra_config)
    g_reasons = {r["reason_code"] for r in present["rows"] if r["output_cut"] == "G"}
    assert "LATE_SUPPLIER" not in g_reasons
