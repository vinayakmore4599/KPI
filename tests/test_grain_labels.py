"""Grain switch, data_points, week buckets, labels, sign_label, meta, green.

What this file provides
    Allowlisted time_grain, source_grain, calendar vs period trailing,
    ISO Monday weeks, unique English trend_labels, sign_label, SelectedMetrics
    vs explicit [], and green_when thresholds.

Where it is used
    pytest tests/test_grain_labels.py.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.capabilities.functions.measure.impl import sign_label
from kpi_engine.contracts import TimeSpec
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.dates import period_label, week_start
from kpi_engine.exceptions import BindError, TimePlanError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _switch_kpi(kpi_id: int, **overrides):
    """Monthly default with daily source and a day/week/month allowlist."""
    spec = minimal_kpi(
        kpi_id,
        time={
            "column": "event_month",
            "grain": "month",
            "source_grain": "day",
            "grains": ["day", "week", "month"],
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        data_points={"day": 30, "week": 12, "month": 12},
        parameters={"time_grain": {"type": "string"}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "prior_week": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"weeks": 1},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "trend_n": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"from": "data_points"},
                "inclusive": True,
            },
            "value_wtd": {"of": "sotif_value", "op": "window", "range": "wtd"},
        },
    )
    spec.update(overrides)
    return spec


def _daily_facts(path, amounts: dict[date, float] | None = None) -> None:
    """One NA / LATE_SUPPLIER row per day in amounts (default Mon 23–Tue 24 Mar 2026)."""
    if amounts is None:
        amounts = {date(2026, 3, 23): 10.0, date(2026, 3, 24): 5.0}
    rows = [
        {
            "event_month": day.isoformat(),
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": value,
        }
        for day, value in amounts.items()
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_week_pick_is_allowed_year_is_not(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9601.yaml", _switch_kpi(9601))
    ctx = make_context(
        parquet_path, measures=["current_value"], kpi_id=9601, time_grain="week"
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["time_grain"] == "week"

    ctx["parameters"]["time_grain"] = "year"
    with pytest.raises(BindError, match="not allowed"):
        validate(ctx, config_dir=extra_config)


def test_day_pick_rejected_when_source_grain_is_month(parquet_path, extra_config):
    spec = _switch_kpi(9602)
    spec["time"]["source_grain"] = "month"
    write_yaml(extra_config / "kpis" / "9602.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], kpi_id=9602, time_grain="day"
    )
    with pytest.raises(BindError, match="finer than time.source_grain"):
        validate(ctx, config_dir=extra_config)


def test_yyyy_mm_anchor_fails_on_day_pick_truncates_on_week(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9603.yaml", _switch_kpi(9603))
    day_ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9603,
        month="2026-03",
        time_grain="day",
    )
    with pytest.raises(TimePlanError, match="YYYY-MM-DD"):
        validate(day_ctx, config_dir=extra_config)

    week_ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9603,
        month="2026-03",
        time_grain="week",
    )
    planned = validate(week_ctx, config_dir=extra_config)
    assert planned["time_grain"] == "week"
    assert planned["anchor"] == "2026-02-23"


def test_calendar_months_trailing_on_week_pick_is_not_three_weeks(
    parquet_path, extra_config
):
    write_yaml(extra_config / "kpis" / "9604.yaml", _switch_kpi(9604))
    ctx = make_context(
        parquet_path,
        measures=["value_3m"],
        kpi_id=9604,
        month="2026-03-23",
        time_grain="week",
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["time_grain"] == "week"
    assert planned["lookback_months"] >= 8
    assert planned["lookback_months"] != 2


def test_data_points_map_follows_the_pick(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9605.yaml", _switch_kpi(9605))
    day_ctx = make_context(
        parquet_path,
        measures=["trend_n"],
        kpi_id=9605,
        month="2026-03-23",
        time_grain="day",
    )
    assert validate(day_ctx, config_dir=extra_config)["lookback_months"] == 29

    month_ctx = make_context(
        parquet_path,
        measures=["trend_n"],
        kpi_id=9605,
        month="2026-03",
        time_grain="month",
    )
    assert validate(month_ctx, config_dir=extra_config)["lookback_months"] == 11


def test_scalar_data_points_rejected_when_multiple_grains(extra_config):
    spec = _switch_kpi(9606, data_points=12)
    write_yaml(extra_config / "kpis" / "9606.yaml", spec)
    with pytest.raises(BindError, match="must be a map"):
        load_kpi(9606, extra_config)


def test_week_bucket_is_iso_monday_and_offset_weeks_shifts_one_week(
    tmp_path, extra_config
):
    facts = tmp_path / "days.parquet"
    amounts = {
        date(2026, 3, 16): 4.0,
        date(2026, 3, 17): 4.0,
        date(2026, 3, 23): 10.0,
        date(2026, 3, 24): 5.0,
        date(2026, 3, 25): 1.0,
    }
    _daily_facts(facts, amounts)
    write_yaml(extra_config / "kpis" / "9607.yaml", _switch_kpi(9607))
    ctx = make_context(
        facts,
        measures=["current_value", "prior_week"],
        supplier=["ABC"],
        kpi_id=9607,
        month="2026-03-25",
        time_grain="week",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 16.0
    assert value_of(row, "prior_week") == 8.0
    assert week_start(date(2026, 3, 25)) == date(2026, 3, 23)


def test_wtd_fails_on_month_pick(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9608.yaml", _switch_kpi(9608))
    load_kpi(9608, extra_config)
    ctx = make_context(
        parquet_path,
        measures=["value_wtd"],
        supplier=["ABC"],
        kpi_id=9608,
        time_grain="month",
    )
    with pytest.raises(BindError, match="time.grain: day"):
        compute(ctx, config_dir=extra_config)


def test_period_labels_are_unique_english_and_not_locale(monkeypatch):
    day = TimeSpec(column="d", grain="day", filter_code="t")
    week = TimeSpec(column="d", grain="week", filter_code="t")
    month = TimeSpec(column="d", grain="month", filter_code="t")
    quarter = TimeSpec(column="d", grain="quarter", filter_code="t")
    year = TimeSpec(column="d", grain="year", filter_code="t")
    assert period_label(date(2026, 3, 23), day) == "23 Mar"
    assert period_label(date(2026, 3, 24), day) == "24 Mar"
    assert period_label(date(2026, 3, 23), week) == "2026-W13"
    assert period_label(date(2026, 7, 1), month) == "Jul 2026"
    assert period_label(date(2026, 1, 1), quarter) == "2026-Q1"
    assert period_label(date(2026, 6, 1), year) == "2026"

    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert period_label(date(2026, 3, 23), day) == "23 Mar"
    assert period_label(date(2026, 3, 23), week) == "2026-W13"


def test_trend_labels_align_to_iso_axes(tmp_path, extra_config):
    facts = tmp_path / "days.parquet"
    _daily_facts(
        facts,
        {date(2026, 3, d): 1.0 for d in range(20, 26)},
    )
    write_yaml(extra_config / "kpis" / "9609.yaml", _switch_kpi(9609))
    ctx = make_context(
        facts,
        measures=["trend_n"],
        supplier=["ABC"],
        kpi_id=9609,
        month="2026-03-24",
        time_grain="day",
    )
    result = compute(ctx, config_dir=extra_config)
    axes = result["trend_axes"]["trend_n"]
    labels = result["trend_labels"]["trend_n"]
    assert len(axes) == len(labels) == 30
    assert labels[-2] == "23 Mar"
    assert labels[-1] == "24 Mar"
    assert labels[-2] != labels[-1]


def test_sign_label_values():
    assert sign_label(1) == "Positive"
    assert sign_label(-1) == "Negative"
    assert sign_label(0) == "Neutral"
    assert sign_label(None) is None


def test_sign_label_measure_and_alias(parquet_path, extra_config):
    spec = minimal_kpi(
        9610,
        measures={
            "pos": {"op": "constant", "value": 1},
            "neg": {"op": "constant", "value": -1},
            "zero": {"op": "constant", "value": 0},
            "missing": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 20},
            },
            "up": {"op": "fn", "fn": "sign_label", "inputs": ["pos"]},
            "down": {"op": "fn", "fn": "change_direction", "inputs": ["neg"]},
            "flat": {"op": "fn", "fn": "sign_label", "inputs": ["zero"]},
            "blank": {"op": "fn", "fn": "sign_label", "inputs": ["missing"]},
        },
    )
    write_yaml(extra_config / "kpis" / "9610.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["up", "down", "flat", "blank"],
        supplier=["ABC"],
        kpi_id=9610,
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["up"] == "Positive"
    assert row["down"] == "Negative"
    assert row["flat"] == "Neutral"
    assert row["blank"] is None


def test_omitted_measures_use_selected_metrics(parquet_path, extra_config):
    spec = minimal_kpi(
        9611,
        meta={"KPI": "Sotif", "ParentKPI": "Quality", "IsChild": False,
              "SelectedMetrics": ["current_value", "value_3m"]},
    )
    write_yaml(extra_config / "kpis" / "9611.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9611)
    ctx["execution"]["view_details"][0].pop("measures_required", None)
    result = compute(ctx, config_dir=extra_config)
    row = result["rows"][0]
    assert "current_value" in row
    assert "value_3m" in row
    assert result["meta"]["KPI"] == "Sotif"
    assert result["meta"]["SelectedMetrics"] == ["current_value", "value_3m"]


def test_explicit_empty_measures_ignores_selected_metrics(parquet_path, extra_config):
    spec = minimal_kpi(
        9612,
        meta={"SelectedMetrics": ["current_value", "value_3m"]},
    )
    write_yaml(extra_config / "kpis" / "9612.yaml", spec)
    ctx = make_context(parquet_path, measures=[], kpi_id=9612)
    result = compute(ctx, config_dir=extra_config)
    for row in result["rows"]:
        assert "current_value" not in row
        assert "value_3m" not in row


def test_green_when_above_and_below(tmp_path, extra_config):
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": reason,
                "supplier_name": "ABC",
                "amount": amount,
            }
            for reason, amount in (("LOW", 9), ("EDGE", 10), ("HIGH", 11))
        ]
    )
    path = tmp_path / "green.parquet"
    frame.to_parquet(path, index=False)
    above = minimal_kpi(
        9613,
        dimensions=[{"name": "reason_code", "kind": "dimension"}],
        cuts=[{"name": "G", "group_by": [], "ignore_filters": []}],
        default_cut="G",
        green_when={"above": 10, "of": "current_value"},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9613.yaml", above)
    ctx = make_context(path, measures=["current_value"], kpi_id=9613)
    rows = {r["reason_code"]: r for r in compute(ctx, config_dir=extra_config)["rows"]}
    assert rows["EDGE"]["green"] is True
    assert rows["HIGH"]["green"] is True
    assert rows["LOW"]["green"] is False

    below = dict(above)
    below["kpi_id"] = 9614
    below["green_when"] = {"below": 5, "of": "current_value"}
    write_yaml(extra_config / "kpis" / "9614.yaml", below)
    ctx_b = make_context(path, measures=["current_value"], kpi_id=9614)
    rows_b = {r["reason_code"]: r for r in compute(ctx_b, config_dir=extra_config)["rows"]}
    assert rows_b["LOW"]["green"] is False
    assert all(row["green"] is False for row in rows_b.values())

    floor = dict(above)
    floor["kpi_id"] = 9615
    floor["green_when"] = {"below": 9, "of": "current_value"}
    write_yaml(extra_config / "kpis" / "9615.yaml", floor)
    ctx_f = make_context(path, measures=["current_value"], kpi_id=9615)
    rows_f = {r["reason_code"]: r for r in compute(ctx_f, config_dir=extra_config)["rows"]}
    assert rows_f["LOW"]["green"] is True
    assert rows_f["EDGE"]["green"] is False


def test_green_when_of_is_computed_even_when_not_requested(parquet_path, extra_config):
    spec = minimal_kpi(
        9616,
        green_when={"above": 10, "of": "current_value"},
    )
    write_yaml(extra_config / "kpis" / "9616.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["value_3m"], supplier=["ABC"], kpi_id=9616
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert "green" in row
    assert row["green"] in (True, False)
    assert "current_value" in row
    assert result["meta"]["of"] == "current_value"
    assert value_of(result["meta"], "above") == 10.0
