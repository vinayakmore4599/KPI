from kpi_engine.core.orchestrator import validate
from tests.conftest import make_context


def test_month_filter_is_range_not_in(parquet_path, config_dir):
    ctx = make_context(parquet_path, measures=["previous_year_value", "value_3m"], supplier=["ABC"])
    plan = validate(ctx, config_dir=config_dir)
    sql = plan["sql"]
    compact = " ".join(sql.split())
    assert '"event_month" IN' not in compact
    assert "date_trunc('month'" in compact
    assert ">=" in compact
    assert "reporting_month" not in compact
    assert plan["lookback_months"] == 12
    assert plan["span_start"] == "2025-03-01"
