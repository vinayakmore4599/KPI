"""Time-planner tests: lookback months and month-filter claim.

What this file provides
    Asserts 3m vs previous_year vs trend lookbacks, and that reporting_month
    becomes the anchor and is removed from remaining filters.

Where it is used
    pytest tests/test_span.py.

When to use
    Add cases when a new measure op changes required_span.
"""

from datetime import date

from kpi_engine.core.binder import load_kpi
from kpi_engine.core.time_planner import lookback_for, max_lookback_months, plan_time
from kpi_engine.core.adapter import adapt
from tests.conftest import make_context


def test_lookback_window_and_previous_year(config_dir):
    """Lookback is calendar months from requested measures only."""
    kpi = load_kpi(3004, config_dir)
    by_key = {o.key: o for o in kpi.measures}
    assert lookback_for(by_key["value_3m"], by_key) == 2
    assert lookback_for(by_key["previous_year_value"], by_key) == 12
    assert lookback_for(by_key["trend_12m"], by_key) == 11
    assert max_lookback_months(kpi, ("value_3m",)) == 2
    assert max_lookback_months(kpi, ("value_3m", "previous_year_value")) == 12
    assert max_lookback_months(kpi, ("trend_12m",)) == 11


def test_anchor_from_month_filter(parquet_path, config_dir):
    """reporting_month is claimed as the anchor and removed from remaining filters."""
    ctx = make_context(parquet_path, measures=["value_3m"], month="2026-03")
    request = adapt(ctx)
    kpi = load_kpi(3004, config_dir)
    plan, rest = plan_time(request, kpi)
    assert plan.anchor == date(2026, 3, 1)
    assert plan.span_start == date(2026, 1, 1)
    assert plan.span_end_exclusive == date(2026, 4, 1)
    assert all(f.code != "reporting_month" for f in rest)
