"""Compose segregated context filters into one period or column value.

What this file provides
    Template concat (`{year}{month:02}`, `{year}/{month:02}`), leftover key
    stripping, lookback still widening from requested measures.

Where it is used
    pytest tests/test_compose_filters.py.

When to use
    Add a case when compose.template syntax or claim order changes.
"""

from datetime import date

import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.adapter import adapt
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.compose import expand_compose, parse_compose_template
from kpi_engine.pipeline.time_planner import plan_time
from kpi_engine.dates import parse_date
from kpi_engine.exceptions import BindError, TimePlanError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _ctx_year_month(parquet_path, extra_config, kpi_id, *, year=2026, month=7, measures=None, extra=None):
    """Context with year/month instead of reporting_month."""
    spec = extra if extra is not None else minimal_kpi(kpi_id)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=measures or ["current_value"],
        supplier=["ABC"],
        kpi_id=kpi_id,
        extra_filters={
            "year": {"values": [year], "input_text": "simple"},
            "month": {"values": [month], "input_text": "simple"},
        },
    )
    del ctx["filters"]["reporting_month"]
    return ctx


def test_expand_compose_pads_and_keeps_literals():
    """Concat is literal except `{name:02}` zero-pad."""
    from kpi_engine.contracts import IncomingFilter

    filters = (
        IncomingFilter(raw_key="year", code="year", values=(2026,), input_text=None),
        IncomingFilter(raw_key="month", code="month", values=(7,), input_text=None),
    )
    compact, consumed = expand_compose("{year}{month:02}", filters)
    assert compact == "202607"
    assert set(consumed) == {"year", "month"}
    slash, _ = expand_compose("{year}/{month:02}", filters)
    assert slash == "2026/07"
    raw, _ = expand_compose("{year}/{month}", filters)
    assert raw == "2026/7"


def test_parse_compose_rejects_one_placeholder_and_unmatched_brace():
    """A template must name at least two context keys and close every `{`."""
    with pytest.raises(BindError, match="at least two context filters"):
        parse_compose_template("{year}")
    with pytest.raises(BindError, match="unmatched"):
        parse_compose_template("{year}{month")
    with pytest.raises(BindError, match="not a filter code"):
        parse_compose_template("{month:02d}{year}")


def test_compose_yyyymm_is_the_anchor_and_parts_are_stripped(parquet_path, extra_config):
    """year=2026, month=7 → 202607; leftover has no year/month; SQL is a range."""
    spec = minimal_kpi(9890)
    spec["time"]["format"] = "yyyymm"
    spec["time"]["compose"] = {"template": "{year}{month:02}"}
    ctx = _ctx_year_month(parquet_path, extra_config, 9890, extra=spec)
    plan, rest = plan_time(adapt(ctx), load_kpi(9890, extra_config))
    assert plan.anchor == date(2026, 7, 1)
    assert {f.code for f in rest} <= {"Supplier Name"}
    assert all(f.code not in {"year", "month", "reporting_month"} for f in rest)


def test_compose_slash_month_parses_with_yyyy_mm(parquet_path, extra_config):
    """`{year}/{month:02}` + format yyyy/mm is still one selected period."""
    spec = minimal_kpi(9891)
    spec["time"]["format"] = "yyyy/mm"
    spec["time"]["compose"] = {"template": "{year}/{month:02}"}
    ctx = _ctx_year_month(parquet_path, extra_config, 9891, extra=spec)
    assert parse_date("2026/07", fmt="yyyy/mm") == date(2026, 7, 1)
    plan, rest = plan_time(adapt(ctx), load_kpi(9891, extra_config))
    assert plan.anchor == date(2026, 7, 1)
    assert all(f.code not in {"year", "month"} for f in rest)


def test_compose_three_part_day_anchor(parquet_path, extra_config):
    """year + month + day concatenates to a day-grain anchor."""
    spec = minimal_kpi(9892)
    spec["time"]["grain"] = "day"
    spec["time"]["format"] = "yyyy-mm-dd"
    spec["time"]["compose"] = {"template": "{year}-{month:02}-{day:02}"}
    write_yaml(extra_config / "kpis" / "9892.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9892)
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["year"] = {"values": [2026], "input_text": "simple"}
    ctx["filters"]["month"] = {"values": [3], "input_text": "simple"}
    ctx["filters"]["day"] = {"values": [15], "input_text": "simple"}
    plan, rest = plan_time(adapt(ctx), load_kpi(9892, extra_config))
    assert plan.anchor == date(2026, 3, 15)
    assert all(f.code not in {"year", "month", "day"} for f in rest)


def test_compose_does_not_replace_lookback(parquet_path, extra_config):
    """previous_year still widens 12 months after year/month are composed."""
    spec = minimal_kpi(9893)
    spec["time"]["compose"] = {"template": "{year}-{month:02}"}
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"years": 1},
    }
    ctx = _ctx_year_month(
        parquet_path,
        extra_config,
        9893,
        year=2026,
        month=3,
        measures=["current_value", "previous_year_value"],
        extra=spec,
    )
    planned = validate(ctx, config_dir=extra_config)
    assert value_of(planned, "lookback_months") == 12
    assert planned["span_start"] == "2025-03-01"
    sql = " ".join(planned["sql"].split())
    assert " IN (2026" not in sql
    assert '"year" IN' not in sql
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "current_value") == 45.0
    assert value_of(late, "previous_year_value") == 15.0


def test_host_scalar_period_wins_and_compose_keys_are_still_stripped(parquet_path, extra_config):
    """An existing reporting_month value is the anchor; year/month are dropped."""
    spec = minimal_kpi(9894)
    spec["time"]["compose"] = {"template": "{year}-{month:02}"}
    write_yaml(extra_config / "kpis" / "9894.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9894,
        month="2026-03",
        extra_filters={
            "year": {"values": [1999], "input_text": "simple"},
            "month": {"values": [1], "input_text": "simple"},
        },
    )
    plan, rest = plan_time(adapt(ctx), load_kpi(9894, extra_config))
    assert plan.anchor == date(2026, 3, 1)
    assert all(f.code not in {"year", "month"} for f in rest)


def test_missing_compose_part_is_a_time_plan_error(parquet_path, extra_config):
    """Both placeholders must be on the context when filter_code is absent."""
    spec = minimal_kpi(9895)
    spec["time"]["compose"] = {"template": "{year}{month:02}"}
    spec["time"]["format"] = "yyyymm"
    write_yaml(extra_config / "kpis" / "9895.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9895)
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["month"] = {"values": [7], "input_text": "simple"}
    with pytest.raises(TimePlanError, match="year"):
        plan_time(adapt(ctx), load_kpi(9895, extra_config))


def test_non_time_compose_builds_an_extract_filter(parquet_path, extra_config):
    """filters.compose concatenates leftover keys onto a physical column."""
    spec = minimal_kpi(9896)
    spec["filters"] = {
        "reason_code": {
            "column": "reason_code",
            "op": "eq",
            "apply": "extract",
            "compose": {"template": "{reason_a}_{reason_b}"},
        }
    }
    write_yaml(extra_config / "kpis" / "9896.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9896,
        extra_filters={
            "reason_a": {"values": ["LATE"], "input_text": "simple"},
            "reason_b": {"values": ["SUPPLIER"], "input_text": "simple"},
        },
    )
    planned = validate(ctx, config_dir=extra_config)
    assert '"reason_code" = ?' in " ".join(planned["sql"].split())
    result = compute(ctx, config_dir=extra_config)
    assert {r["reason_code"] for r in result["rows"]} == {"LATE_SUPPLIER"}
    applied = {f["filter_code"]: f for f in result["applied_filters"]}
    assert applied["reason_code"]["values"] == ["LATE_SUPPLIER"]
    assert "reason_a" not in applied
    assert "reason_b" not in applied


def test_non_time_compose_skips_when_a_part_is_blank(parquet_path, extra_config):
    """Missing compose placeholders skip the row filter instead of bind-error."""
    spec = minimal_kpi(9897)
    spec["filters"] = {
        "reason_code": {
            "column": "reason_code",
            "op": "eq",
            "apply": "extract",
            "compose": {"template": "{reason_a}_{reason_b}"},
        }
    }
    write_yaml(extra_config / "kpis" / "9897.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9897,
        extra_filters={"reason_a": {"values": ["LATE"], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=extra_config)
    assert value_of(find_row(result, cut="G", reason="LATE_SUPPLIER"), "current_value") == 45.0
    assert {"filter_code": "reason_code", "reason": "blank"} in result["skipped_filters"]
    assert all(f["filter_code"] != "reason_code" for f in result["applied_filters"])
