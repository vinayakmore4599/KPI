"""Anchor and required span: how wide the DuckDB scan has to be.

What this file provides
    plan_time / lookback_for behaviour per measure kind and grain, the
    single-value month filter contract, and the day-grain full-date rule.

Where it is used
    pytest tests/test_lookback_planning.py.

When to use
    Add a case when a new measure op or grain changes the scanned span.
"""

from datetime import date

import pytest

from kpi_engine.contracts import Offset, OutputSpec, TimeSpec
from kpi_engine.pipeline.adapter import adapt
from kpi_engine.pipeline.binder import fold_measure_keys, load_kpi
from kpi_engine.pipeline.time_planner import (
    claim_month_filter,
    lookback_for,
    max_lookback_months,
    plan_time,
)
from kpi_engine.exceptions import TimePlanError
from tests.conftest import make_context, minimal_kpi, write_yaml


def test_dimension_and_zero_offset_measures_need_no_history():
    """Dimension columns and the anchor value itself scan a single period."""
    by_key: dict[str, OutputSpec] = {}
    assert lookback_for(OutputSpec(key="reason_code", kind="dimension"), by_key) == 0
    assert lookback_for(OutputSpec(key="current", kind="point", of="v"), by_key) == 0
    assert (
        lookback_for(OutputSpec(key="current", kind="point", of="v", offset=Offset()), by_key) == 0
    )


@pytest.mark.parametrize(
    ("trailing", "inclusive", "expected"),
    [(3, True, 2), (3, False, 3), (12, True, 11), (12, False, 12), (1, True, 0), (1, False, 1)],
)
def test_window_lookback_depends_on_inclusivity(trailing, inclusive, expected):
    """Inclusive windows count the anchor; exclusive windows sit entirely behind it."""
    spec = OutputSpec(
        key="w", kind="window", of="v", trailing_months=trailing, inclusive=inclusive
    )
    assert lookback_for(spec, {}) == expected


def test_arithmetic_lookback_is_the_deeper_operand(config_dir):
    """A ratio scans as far back as its deepest input."""
    kpi = load_kpi(3004, config_dir)
    by_key = {m.key: m for m in kpi.measures}
    assert lookback_for(by_key["yoy_month"], by_key) == 12
    assert max_lookback_months(kpi, ("yoy_month",)) == 12
    assert max_lookback_months(kpi, ("current_value",)) == 0


def test_hook_lookback_uses_offset_or_trailing():
    """Hook measures declare their own reach through offset or trailing."""
    offset_hook = OutputSpec(key="h", kind="hook", hook="x", offset=Offset(years=1))
    window_hook = OutputSpec(key="h", kind="hook", hook="x", trailing_months=6, inclusive=True)
    exclusive_hook = OutputSpec(key="h", kind="hook", hook="x", trailing_months=6, inclusive=False)
    bare_hook = OutputSpec(key="h", kind="hook", hook="x")
    assert lookback_for(offset_hook, {}) == 12
    assert lookback_for(window_hook, {}) == 5
    assert lookback_for(exclusive_hook, {}) == 6
    assert lookback_for(bare_hook, {}) == 0


def test_hook_offset_lookback_is_converted_to_grain_periods():
    """A hook offset counts in grain periods, like a point measure does."""
    quarterly = TimeSpec(column="event_month", grain="quarter", filter_code="reporting_month")
    spec = OutputSpec(key="h", kind="hook", hook="x", offset=Offset(years=1))
    assert lookback_for(spec, {}, quarterly) == 4


def test_lookback_for_unknown_kind_is_rejected():
    """An op the planner cannot size must fail rather than guess a span."""
    with pytest.raises(TimePlanError, match="Cannot plan lookback"):
        lookback_for(OutputSpec(key="mystery", kind="teleport"), {})


def test_offset_lookback_is_converted_into_grain_periods():
    """On a quarter KPI, a one-year offset is four periods, not twelve."""
    quarterly = TimeSpec(
        column="event_month", grain="quarter", filter_code="reporting_month"
    )
    daily = TimeSpec(column="event_day", grain="day", filter_code="reporting_day")
    year_back = OutputSpec(key="p", kind="point", of="v", offset=Offset(years=1))
    week_back = OutputSpec(key="p", kind="point", of="v", offset=Offset(days=7))
    assert lookback_for(year_back, {}, quarterly) == 4
    assert lookback_for(week_back, {}, daily) == 7


def test_max_lookback_uses_requested_measures_only(config_dir):
    """An unrequested 12-month measure must not widen the scan."""
    kpi = load_kpi(3004, config_dir)
    assert max_lookback_months(kpi, ("current_value", "value_6m")) == 5
    assert max_lookback_months(kpi, ()) == 0


def test_span_covers_lookback_through_the_anchor(parquet_path, config_dir):
    """span_start is lookback periods back; span_end is exclusive of the next period."""
    ctx = make_context(parquet_path, measures=["value_6m"], month="2026-03")
    plan, _rest = plan_time(adapt(ctx), load_kpi(3004, config_dir))
    assert plan.anchor == date(2026, 3, 1)
    assert plan.span_start == date(2025, 10, 1)
    assert plan.span_end_exclusive == date(2026, 4, 1)
    assert plan.lookback_months == 5
    assert plan.claimed_filter_code == "reporting_month"


def test_anchor_is_truncated_to_the_kpi_grain(parquet_path, extra_config):
    """A mid-month selected date still anchors on the period start."""
    write_yaml(extra_config / "kpis" / "9200.yaml", minimal_kpi(9200))
    ctx = make_context(parquet_path, measures=["current_value"], month="2026-03-17", kpi_id=9200)
    plan, _rest = plan_time(adapt(ctx), load_kpi(9200, extra_config))
    assert plan.anchor == date(2026, 3, 1)


def test_month_filter_must_carry_exactly_one_value(parquet_path, config_dir):
    """The selected period is a single anchor, never a multi-select IN list."""
    kpi = load_kpi(3004, config_dir)
    for values in (["2026-02", "2026-03"], []):
        ctx = make_context(parquet_path, measures=["current_value"])
        ctx["filters"]["reporting_month"] = {"value": values, "input_text": "simple"}
        with pytest.raises(TimePlanError, match="exactly one value"):
            plan_time(adapt(ctx), kpi)


def test_missing_month_filter_is_whole_history(parquet_path, config_dir):
    """No selected period means probe the data; the engine does not fall back to today."""
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["filters"]["reporting_month"]
    plan, _rest = plan_time(adapt(ctx), load_kpi(3004, config_dir))
    assert plan.anchor is None
    assert plan.selection is not None
    assert plan.selection.anchor_source == "data"


def test_day_grain_requires_a_full_date(parquet_path, extra_config):
    """A day-grain KPI cannot anchor on YYYY-MM."""
    spec = minimal_kpi(
        9201,
        time={
            "column": "event_month",
            "grain": "day",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
    )
    write_yaml(extra_config / "kpis" / "9201.yaml", spec)
    kpi = load_kpi(9201, extra_config)

    ctx = make_context(parquet_path, measures=["current_value"], month="2026-03", kpi_id=9201)
    with pytest.raises(TimePlanError, match="requires a full date"):
        plan_time(adapt(ctx), kpi)

    ctx = make_context(parquet_path, measures=["current_value"], month="2026-03-17", kpi_id=9201)
    plan, _rest = plan_time(adapt(ctx), kpi)
    assert plan.anchor == date(2026, 3, 17)


def test_claim_month_filter_matches_on_code_or_raw_key(parquet_path):
    """The time filter is found case-insensitively and removed from the IN filters."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    ctx["filters"]["Reporting Month"] = ctx["filters"].pop("reporting_month")
    filters = adapt(ctx).filters
    claimed, rest = claim_month_filter(filters, "reporting_month")
    assert claimed is not None
    assert claimed.raw_key == "Reporting Month"
    assert [f.code for f in rest] == ["Supplier Name"]


def test_yyyymm_previous_year_widens_from_202607(parquet_path, extra_config):
    """Filter 202607 plus previous_year_value scans Jul 2025 through Jul 2026."""
    spec = minimal_kpi(9210)
    spec["time"]["format"] = "yyyymm"
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"years": 1},
    }
    write_yaml(extra_config / "kpis" / "9210.yaml", spec)
    kpi = load_kpi(9210, extra_config)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "Previous_Year_Value"],
        month="202607",
        kpi_id=9210,
    )
    assert fold_measure_keys(kpi, adapt(ctx).measure_keys) == (
        "current_value",
        "previous_year_value",
    )
    plan, _rest = plan_time(adapt(ctx), kpi)
    assert plan.anchor == date(2026, 7, 1)
    assert plan.span_start == date(2025, 7, 1)
    assert plan.span_end_exclusive == date(2026, 8, 1)
    assert plan.lookback_months == 12
    assert max_lookback_months(kpi, ("Previous_Year_Value",)) == 12
    assert max_lookback_months(kpi, ("previousyearvalue",)) == 12


def test_claim_month_filter_returns_none_when_absent(parquet_path):
    """Nothing is claimed when the KPI's filter_code is not on the context."""
    filters = adapt(make_context(parquet_path, measures=["current_value"])).filters
    claimed, rest = claim_month_filter(filters, "fiscal_period")
    assert claimed is None
    assert len(rest) == len(filters)
