"""Independent time-part filters: year/month/quarter selections and output_cut walk."""

from datetime import date

import pytest

from kpi_engine import compute, validate
from kpi_engine.contracts import Offset, TimeSpec
from kpi_engine.exceptions import BindError, TimePlanError
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.period_select import (
    parse_part_values,
    selection_bounds,
    selection_periods,
    shift_selection,
)
from kpi_engine.pipeline.time_planner import plan_time
from kpi_engine.pipeline.adapter import adapt
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _periods_kpi(kpi_id: int, **overrides):
    spec = minimal_kpi(
        kpi_id,
        time={
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
            "periods": {
                "year": "year",
                "quarter": "quarter",
                "month": "current month",
            },
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "trend_12m": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 12},
                "inclusive": True,
            },
            "yoy_growth": {
                "of": "current_value",
                "op": "pct_change",
                "offset": {"years": 1},
            },
        },
    )
    spec.update(overrides)
    if "time" in overrides:
        spec["time"] = overrides["time"]
    if "measures" in overrides:
        spec["measures"] = overrides["measures"]
    return spec


def _year_month_context(parquet_path, kpi_id, *, year=None, month=None, measures=None):
    ctx = make_context(
        parquet_path,
        measures=measures or ["current_value", "previous_year_value", "value_3m", "trend_12m"],
        supplier=["ABC"],
        kpi_id=kpi_id,
    )
    del ctx["filters"]["reporting_month"]
    extra = {}
    if year is not None:
        extra["year"] = {"value": year if isinstance(year, list) else [year], "input_text": "simple"}
    if month is not None:
        extra["current month"] = {
            "value": month if isinstance(month, list) else [month],
            "input_text": "simple",
        }
    ctx["filters"].update(extra)
    return ctx


def test_parse_part_values_accepts_padded_ints():
    assert parse_part_values("month", ("03", 3, "3")) == (3,)
    with pytest.raises(TimePlanError, match="integer"):
        parse_part_values("month", ("March",))


def test_year_only_materializes_calendar_year():
    time = TimeSpec(column="event_month", grain="month", filter_code="", calendar="gregorian")
    parts = {"year": (2026,)}
    bounds = selection_bounds(parts, time)
    periods = selection_periods(parts, bounds, time)
    assert periods[0] == date(2026, 1, 1)
    assert periods[-1] == date(2026, 12, 1)
    assert len(periods) == 12
    shifted = shift_selection(periods, Offset(years=-1), time)
    assert shifted[0] == date(2025, 1, 1)
    assert shifted[-1] == date(2025, 12, 1)


def test_periods_and_compose_are_a_bind_error(extra_config):
    spec = _periods_kpi(9601)
    spec["time"]["compose"] = {"template": "{year}{month:02}"}
    write_yaml(extra_config / "kpis" / "9601.yaml", spec)
    with pytest.raises(BindError, match="cannot both be set"):
        load_kpi(9601, extra_config)


def test_finer_period_part_is_a_bind_error(extra_config):
    spec = _periods_kpi(9602)
    spec["time"]["periods"]["week"] = "iso_week"
    write_yaml(extra_config / "kpis" / "9602.yaml", spec)
    with pytest.raises(BindError, match="finer than time.grain"):
        load_kpi(9602, extra_config)


def test_scalar_filter_code_unchanged(parquet_path, extra_config):
    """Leaving reporting_month on the context keeps today's single-bucket numbers."""
    write_yaml(extra_config / "kpis" / "9603.yaml", _periods_kpi(9603))
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9603,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    assert g["previous_year_value"] == 15.0
    assert g["value_3m"] == 90.0
    assert result["parameters"]["time_selection"]["anchor_source"] == "legacy"
    planned = validate(ctx, config_dir=extra_config)
    assert '"event_month" IN' not in " ".join(planned["sql"].split())
    assert planned["span_start"] == "2025-03-01"


def test_year_and_month_june(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9604.yaml", _periods_kpi(9604))
    ctx = _year_month_context(parquet_path, 9604, year=2026, month="06")
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-06-01"
    assert g["current_value"] is None
    assert g["previous_year_value"] == 90.0
    assert g["value_3m"] == 0.0
    axis = result["trend_axes"]["trend_12m"]
    assert axis[0] == "2025-07-01"
    assert axis[-1] == "2026-06-01"


def test_year_only_folds_and_shifts_the_year(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9605.yaml", _periods_kpi(9605))
    ctx = _year_month_context(
        parquet_path,
        9605,
        year=2026,
        measures=["current_value", "previous_year_value", "value_3m", "trend_12m", "yoy_growth"],
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-12-01"
    assert result["parameters"]["span_start"] == "2025-01-01"
    assert result["parameters"]["time_selection"]["anchor_source"] == "context"
    assert g["current_value"] == 90.0
    assert g["previous_year_value"] == 1140.0
    assert g["value_3m"] == 0.0
    axis = result["trend_axes"]["trend_12m"]
    assert axis[0] == "2026-01-01"
    assert axis[-1] == "2026-12-01"
    assert len(g["trend_12m"]) == 12
    assert g["trend_12m"][0] == 15.0
    assert g["trend_12m"][2] == 45.0
    assert g["yoy_growth"] == pytest.approx((90.0 - 1140.0) / 1140.0)


def test_month_only_is_every_matching_month(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9606.yaml", _periods_kpi(9606))
    ctx = _year_month_context(parquet_path, 9606, month=3)
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-03-01"
    assert result["parameters"]["time_selection"]["anchor_source"] == "data"
    assert g["current_value"] == 60.0
    assert g["previous_year_value"] == 15.0
    assert g["value_3m"] == 90.0


def test_quarter_part_gregorian_and_fiscal(parquet_path, extra_config):
    spec = _periods_kpi(9607)
    write_yaml(extra_config / "kpis" / "9607.yaml", spec)
    ctx = _year_month_context(parquet_path, 9607, year=2026)
    ctx["filters"]["quarter"] = {"value": [1], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-03-01"
    assert g["current_value"] == 90.0

    fiscal = _periods_kpi(
        9608,
        time={
            "column": "event_month",
            "grain": "month",
            "calendar": "fiscal",
            "fiscal_start_month": 4,
            "periods": {"year": "year", "quarter": "quarter"},
        },
    )
    write_yaml(extra_config / "kpis" / "9608.yaml", fiscal)
    fctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9608
    )
    del fctx["filters"]["reporting_month"]
    fctx["filters"]["year"] = {"value": [2025], "input_text": "simple"}
    fctx["filters"]["quarter"] = {"value": [2], "input_text": "simple"}
    planned = validate(fctx, config_dir=extra_config)
    assert planned["anchor"] == "2025-09-01"


def test_week_part_is_iso_only():
    time = TimeSpec(column="event_month", grain="week", filter_code="", calendar="gregorian")
    parts = {"year": (2026,), "week": (10,)}
    periods = selection_periods(parts, selection_bounds(parts, time), time)
    assert periods == (date(2026, 3, 2),)


def test_empty_selection_month_day_impossible(parquet_path, extra_config):
    spec = minimal_kpi(
        9609,
        time={
            "column": "event_month",
            "grain": "day",
            "source_grain": "day",
            "periods": {"year": "year", "month": "current month", "day": "day"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "trend_12m": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 12},
                "inclusive": True,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9609.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "trend_12m"],
        supplier=["ABC"],
        kpi_id=9609,
    )
    del ctx["filters"]["reporting_month"]
    ctx["filters"]["year"] = {"value": [2026], "input_text": "simple"}
    ctx["filters"]["current month"] = {"value": [2], "input_text": "simple"}
    ctx["filters"]["day"] = {"value": [30], "input_text": "simple"}
    plan, _rest = plan_time(adapt(ctx), load_kpi(9609, extra_config))
    assert plan.anchor is None
    assert plan.selection.empty_reason
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["anchor"] is None
    assert result["rows"] == []
    assert result["trend_axes"].get("trend_12m") in (None, [])
    assert any(n.get("code") == "empty_time_selection" for n in result["notes"])


def test_g2_explicit_selection_collapses(parquet_path, extra_config):
    """Passing selection=(one month,) folds that month, not the whole year."""
    write_yaml(extra_config / "kpis" / "9610.yaml", _periods_kpi(9610))
    ctx = _year_month_context(parquet_path, 9610, year=2026, month=3)
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    assert len(g["trend_12m"]) == 12
    assert len({v for v in g["trend_12m"] if v}) >= 3


def test_year_list_folds_both_years(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9612.yaml", _periods_kpi(9612))
    ctx = _year_month_context(parquet_path, 9612, year=[2025, 2026])
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-12-01"
    assert g["current_value"] == 1230.0


def test_year_and_month_list_is_q1(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9613.yaml", _periods_kpi(9613))
    ctx = _year_month_context(parquet_path, 9613, year=2026, month=[1, 2, 3])
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert result["parameters"]["anchor"] == "2026-03-01"
    assert g["current_value"] == 90.0


def test_no_time_parts_is_whole_history(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9614.yaml", _periods_kpi(9614))
    ctx = _year_month_context(parquet_path, 9614)
    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["time_selection"]["anchor_source"] == "data"
    assert result["parameters"]["anchor"] == "2026-03-01"
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 1230.0


def test_pack_also_emit_false_locks_one_cut(parquet_path, extra_config):
    spec = minimal_kpi(
        9611,
        parameters={"output_cut": {"type": "string", "allowed": ["G", "R"]}},
    )
    spec["cuts"][0]["pack_also_emit"] = False
    write_yaml(extra_config / "kpis" / "9611.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9611,
        parameters={"output_cut": "G"},
    )
    result = compute(ctx, config_dir=extra_config)
    assert {row["output_cut"] for row in result["rows"]} == {"G"}
