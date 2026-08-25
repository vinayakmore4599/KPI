"""Context columns drive the extract; only the requested measure graph is calculated.

What this file provides
    SQL contains context fact columns (amount) even with several IN filters.
    Unused bases stay out of the retrieve and the payload.
    yoy_month pulls current + previous year but does not emit them.
    A needed fact missing from context.datasets[].columns is a bind error.
    model: Sotif still attaches to model_id sotif.

Where it is used
    pytest tests/test_context_select_and_graph.py.

When to use
    Add a case when retrieve-list or measure-graph scoping changes.
"""

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import resolve_requested_graph, load_kpi
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_context_amount_is_in_sql_with_filters(parquet_path, config_dir):
    """Host-listed amount is projected even when several filters sit on WHERE."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
    )
    sql = validate(ctx, config_dir=config_dir)["sql"]
    assert '"amount"' in sql
    assert '"supplier_name" IN (?)' in sql
    assert '"region" IN (?)' in sql or "region" in sql.lower()


def test_unused_base_column_is_not_required(parquet_path, extra_config):
    """Requesting current_value does not retrieve or require an unused delay_days base."""
    spec = minimal_kpi(9860)
    spec["base_measures"] = {
        "sotif_value": {"sql": "amount", "agg": "sum"},
        "delay": {"sql": "delay_days", "agg": "sum"},
    }
    spec["measures"]["delay_now"] = {
        "of": "delay",
        "op": "point",
        "offset": {"months": 0},
    }
    write_yaml(extra_config / "kpis" / "9860.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9860)
    planned = validate(ctx, config_dir=extra_config)
    assert '"amount"' in planned["sql"]
    assert "delay_days" not in planned["sql"]
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["current_value"] == 30.0
    assert "delay_now" not in row
    assert "trend_12m" not in row


def test_yoy_computes_deps_but_emits_only_yoy(parquet_path, config_dir):
    """yoy_month needs current and previous year; those keys stay off the row."""
    ctx = make_context(parquet_path, measures=["yoy_month"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    row = find_row(result, cut="R", reason="OTHER", region="NA")
    assert "yoy_month" in row
    assert row["yoy_month"] is not None
    assert "current_value" not in row
    assert "previous_year_value" not in row
    assert "trend_12m" not in row


def test_missing_context_fact_column_fails(parquet_path, extra_config):
    """A needed amount column that context did not list is a bind error, not zeros."""
    write_yaml(extra_config / "kpis" / "9861.yaml", minimal_kpi(9861))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9861)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
    ]
    try:
        validate(ctx, config_dir=extra_config)
    except BindError as exc:
        assert "amount" in str(exc)
        assert "context.datasets" in str(exc)
    else:
        raise AssertionError("expected BindError for missing amount")


def test_model_id_fold_still_selects_context_columns(parquet_path, extra_config):
    """base_measures.model Sotif attaches to model_id sotif and still SELECTs amount."""
    spec = minimal_kpi(9862)
    spec["base_measures"] = {
        "sotif_value": {"sql": "amount", "agg": "sum", "model": "Sotif"},
    }
    write_yaml(extra_config / "kpis" / "9862.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9862)
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert '"amount"' in sql
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["current_value"] == 30.0


def test_resolve_requested_graph_yoy_needs_sotif_value(config_dir):
    """Resolver walks yoy → current/previous year → sotif_value only."""
    kpi = load_kpi(3004, config_dir)
    emit, bases = resolve_requested_graph(kpi, ("yoy_month",))
    assert emit == ("yoy_month",)
    assert [base.name for base in bases] == ["sotif_value"]
    emit_cur, bases_cur = resolve_requested_graph(kpi, ("current_value",))
    assert emit_cur == ("current_value",)
    assert [base.name for base in bases_cur] == ["sotif_value"]
    emit_none, bases_none = resolve_requested_graph(kpi, ())
    assert emit_none == ()
    assert bases_none == ()
