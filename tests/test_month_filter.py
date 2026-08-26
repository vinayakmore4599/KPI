"""SQL-shape tests: selected month must be a range, not IN.

What this file provides
    validate() compile check that event_month is not IN-filtered.

Where it is used
    pytest tests/test_month_filter.py.

When to use
    Keep this test if anyone changes model_sql WHERE generation.
"""
from kpi_engine.pipeline.orchestrator import validate
from tests.conftest import make_context, value_of


def test_month_filter_is_range_not_in(parquet_path, config_dir):
    """Generated SQL must range-filter time, never IN the selected month."""
    ctx = make_context(parquet_path, measures=["previous_year_value", "value_3m"], supplier=["ABC"])
    plan = validate(ctx, config_dir=config_dir)
    sql = plan["sql"]
    compact = " ".join(sql.split())
    assert '"event_month" IN' not in compact
    assert "date_trunc('month'" in compact
    assert ">=" in compact
    assert "reporting_month" not in compact
    assert value_of(plan, "lookback_months") == 12
    assert plan["span_start"] == "2025-03-01"
