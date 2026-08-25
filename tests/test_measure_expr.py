"""base_measures.sql may multiply (or add/divide) columns, then agg: sums that.

What this file provides
    compile_sql_expr accepts ontime * fullqty; injection-shaped SQL is rejected.
    compute SUM(amount * 2) equals twice SUM(amount).

Where it is used
    pytest tests/test_measure_expr.py.

When to use
    Add a case if a new operator is allowed in sql: (today + - * / only).
"""

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import compile_sql_expr
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_compile_sql_expr_quotes_column_math():
    """ontime * fullqty becomes quoted DuckDB, not a raw SQL string."""
    assert compile_sql_expr("ontime * fullqty") == '"ontime" * "fullqty"'
    assert compile_sql_expr("amount * 2") == '"amount" * 2'
    assert compile_sql_expr("(a + b) / c") == '( "a" + "b" ) / "c"'


@pytest.mark.parametrize(
    "raw",
    [
        "amount; DROP TABLE x",
        "amount -- comment",
        "'secret'",
        "1 + 2",
        "",
        "ontime *",
        "CASE WHEN a THEN 1",
    ],
)
def test_compile_sql_expr_rejects_unsafe_or_incomplete(raw):
    """Comments, injection, number-only, and incomplete CASE still fail."""
    with pytest.raises(BindError, match="Illegal measure sql"):
        compile_sql_expr(raw, what="measure sql")


def test_compile_sql_expr_allows_allowlisted_calls():
    assert compile_sql_expr("coalesce(amount, 0)") == "coalesce(\"amount\", 0)"
    assert "CASE" in compile_sql_expr("CASE WHEN amount IS NULL THEN 0 ELSE amount END")


def test_sum_of_column_times_column(parquet_path, extra_config):
    """agg: sum with sql: amount * 2 is SUM(amount * 2), twice the usual current_value."""
    doubled = minimal_kpi(
        9710,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    doubled["base_measures"]["sotif_value"]["sql"] = "amount * 2"
    write_yaml(extra_config / "kpis" / "9710.yaml", doubled)
    load_kpi(9710, extra_config)

    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9710
    )
    result = compute(ctx, config_dir=extra_config)
    assert '"amount"' in result["sql"]
    assert "SUM(" not in result["sql"]
    assert '"amount" * 2' not in result["sql"]
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    # 2026-03 NA LATE_SUPPLIER amount is 10 * 3 = 30; doubled = 60.
    assert row["current_value"] == 60.0


def test_product_of_two_fact_columns(tmp_path, extra_config, parquet_path):
    """SUM(ontime * fullqty) is the row-wise product, then a sum — not SUM(ontime)*SUM(fullqty)."""
    frame = pd.DataFrame(
        {
            "event_month": ["2026-03-01", "2026-03-01"],
            "region": ["NA", "NA"],
            "reason_code": ["LATE_SUPPLIER", "LATE_SUPPLIER"],
            "supplier_name": ["ABC", "ABC"],
            "ontime": [1, 0],
            "fullqty": [10, 4],
        }
    )
    path = tmp_path / "product.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9711,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"]["sotif_value"]["sql"] = "ontime * fullqty"
    write_yaml(extra_config / "kpis" / "9711.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9711)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "ontime",
        "fullqty",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    # 1*10 + 0*4 = 10, not (1+0)*(10+4) = 14
    assert row["current_value"] == 10.0


def test_sql_formula_is_not_sent_to_duckdb(parquet_path, extra_config):
    """KPI YAML math stays in Pandas: DuckDB SQL names the column, not the formula."""
    spec = minimal_kpi(9712)
    spec["base_measures"]["sotif_value"]["sql"] = "(amount * 2) / (amount + 1)"
    write_yaml(extra_config / "kpis" / "9712.yaml", spec)
    sql = compute(
        make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9712),
        config_dir=extra_config,
    )["sql"]
    assert "SUM(" not in sql
    assert "*" not in sql.split("FROM")[0]
    assert '"amount"' in sql


def test_row_expr_and_aggregate_expr_are_not_the_same(tmp_path, extra_config):
    """Per-row (a*b)/(a+b) then sum ≠ (sum a * sum b) / (sum a + sum b)."""
    frame = pd.DataFrame(
        {
            "event_month": ["2026-03-01", "2026-03-01"],
            "region": ["NA", "NA"],
            "reason_code": ["LATE_SUPPLIER", "LATE_SUPPLIER"],
            "supplier_name": ["ABC", "ABC"],
            "col_a": [1.0, 3.0],
            "col_b": [1.0, 1.0],
        }
    )
    path = tmp_path / "harmonic.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9713,
        measures={
            "row_ratio": {"of": "row_fact", "op": "point", "offset": {"months": 0}},
            "a_value": {"of": "a_fact", "op": "point", "offset": {"months": 0}},
            "b_value": {"of": "b_fact", "op": "point", "offset": {"months": 0}},
            "agg_ratio": {
                "op": "expr",
                "expr": "(a_value * b_value) / (a_value + b_value)",
            },
        },
    )
    spec["base_measures"] = {
        "row_fact": {"expr": "(col_a * col_b) / (col_a + col_b)", "agg": "sum"},
        "a_fact": {"sql": "col_a", "agg": "sum"},
        "b_fact": {"sql": "col_b", "agg": "sum"},
    }
    write_yaml(extra_config / "kpis" / "9713.yaml", spec)
    ctx = make_context(path, measures=["row_ratio", "agg_ratio"], supplier=["ABC"], kpi_id=9713)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "col_a",
        "col_b",
    ]
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["row_ratio"] == pytest.approx(1.25)  # 0.5 + 0.75
    assert row["agg_ratio"] == pytest.approx(4 * 2 / 6)  # 1.333...
    assert "col_a" in result["sql"]
    assert "SUM(" not in result["sql"]


def test_expr_divide_by_zero_is_null(tmp_path, extra_config):
    """A zero denominator in expr: is null, never inf."""
    frame = pd.DataFrame(
        {
            "event_month": ["2026-03-01"],
            "region": ["NA"],
            "reason_code": ["LATE_SUPPLIER"],
            "supplier_name": ["ABC"],
            "col_a": [10.0],
            "col_b": [0.0],
        }
    )
    path = tmp_path / "zero.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9714,
        measures={
            "current_value": {"of": "ratio_fact", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {"ratio_fact": {"expr": "col_a / col_b", "agg": "sum"}}
    write_yaml(extra_config / "kpis" / "9714.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9714)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "col_a",
        "col_b",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["current_value"] is None


def test_expr_and_sql_together_fail_at_bind(extra_config):
    """expr: is the formula; sql: cannot also be set on the same base measure."""
    spec = minimal_kpi(9715)
    spec["base_measures"]["sotif_value"] = {
        "sql": "amount",
        "expr": "amount * 2",
        "agg": "sum",
    }
    write_yaml(extra_config / "kpis" / "9715.yaml", spec)
    with pytest.raises(BindError, match="cannot also set `sql:`"):
        load_kpi(9715, extra_config)
