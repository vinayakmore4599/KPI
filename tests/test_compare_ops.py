"""Bind-time op: compare sugar — presets, grain guards, parity with pct_change."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.adapter import adapt
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months, plan_time
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_compare_yoy_parity_with_pct_change_and_growth_pct(parquet_path, extra_config):
    """Gold two-point growth_pct, pct_change on the base, and compare mode:yoy match."""
    _write(
        extra_config,
        99101,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
            "yoy_gold": {
                "op": "arithmetic",
                "fn": "growth_pct",
                "left": "current_value",
                "right": "previous_year_value",
            },
            "yoy_pct": {
                "of": "sotif_value",
                "op": "pct_change",
                "offset": {"years": 1},
            },
            "yoy_compare": {"op": "compare", "of": "sotif_value", "mode": "yoy"},
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["yoy_gold", "yoy_pct", "yoy_compare"],
        supplier=["ABC"],
        kpi_id=99101,
        month="2026-03",
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    gold = value_of(row, "yoy_gold")
    assert gold is not None
    assert value_of(row, "yoy_pct") == pytest.approx(gold)
    assert value_of(row, "yoy_compare") == pytest.approx(gold)


def test_compare_desugars_to_pct_change_with_years_offset(extra_config):
    _write(
        extra_config,
        99102,
        measures={"yoy": {"op": "compare", "of": "sotif_value", "mode": "yoy"}},
    )
    kpi = load_kpi(99102, extra_config)
    spec = {m.key: m for m in kpi.measures}["yoy"]
    assert spec.kind == "pct_change"
    assert spec.of == "sotif_value"
    assert spec.offset.years == 1


@pytest.mark.parametrize(
    ("mode", "grain", "unit", "value"),
    [
        ("mom", "month", "months", 1),
        ("wow", "week", "weeks", 1),
        ("qoq", "quarter", "periods", 1),
        ("pop", "month", "periods", 1),
    ],
)
def test_compare_preset_offsets(extra_config, mode, grain, unit, value):
    _write(
        extra_config,
        99110,
        time={
            "column": "event_month",
            "grain": grain,
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        measures={"chg": {"op": "compare", "of": "sotif_value", "mode": mode}},
    )
    kpi = load_kpi(99110, extra_config)
    spec = {m.key: m for m in kpi.measures}["chg"]
    assert spec.kind == "pct_change"
    assert getattr(spec.offset, unit) == value


def test_compare_diff_versus_desugars(extra_config):
    _write(
        extra_config,
        99111,
        measures={
            "gap": {
                "op": "compare",
                "of": "sotif_value",
                "mode": "diff",
                "versus": {"years": 1},
            }
        },
    )
    kpi = load_kpi(99111, extra_config)
    spec = {m.key: m for m in kpi.measures}["gap"]
    assert spec.kind == "diff"
    assert spec.offset.years == 1


def test_compare_pct_change_mode_versus(extra_config):
    _write(
        extra_config,
        99112,
        measures={
            "chg": {
                "op": "compare",
                "of": "sotif_value",
                "mode": "pct_change",
                "versus": {"months": 3},
            }
        },
    )
    kpi = load_kpi(99112, extra_config)
    spec = {m.key: m for m in kpi.measures}["chg"]
    assert spec.kind == "pct_change"
    assert spec.offset.months == 3


def test_compare_of_window_matches_pct_change(parquet_path, extra_config):
    """YoY of a 3m window is that window vs the same window one year earlier."""
    _write(
        extra_config,
        99113,
        measures={
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "yoy_window": {"op": "pct_change", "of": "value_3m", "offset": {"years": 1}},
            "yoy_compare": {"op": "compare", "of": "value_3m", "mode": "yoy"},
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["yoy_window", "yoy_compare"],
        supplier=["ABC"],
        kpi_id=99113,
        month="2026-03",
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "yoy_compare") == pytest.approx(value_of(row, "yoy_window"))


def test_compare_yoy_lookback_is_twelve_months(parquet_path, extra_config):
    _write(
        extra_config,
        99114,
        measures={"yoy": {"op": "compare", "of": "sotif_value", "mode": "yoy"}},
    )
    kpi = load_kpi(99114, extra_config)
    assert max_lookback_months(kpi, ("yoy",)) == 12
    ctx = make_context(
        parquet_path, measures=["yoy"], supplier=["ABC"], kpi_id=99114, month="2026-03"
    )
    plan, _ = plan_time(adapt(ctx), kpi)
    assert plan.lookback_months == 12


def test_compare_where_on_base_clones(extra_config):
    _write(
        extra_config,
        99115,
        measures={
            "late_yoy": {
                "op": "compare",
                "of": "sotif_value",
                "mode": "yoy",
                "where": {"column": "reason_code", "op": "eq", "value": "LATE_SUPPLIER"},
            }
        },
    )
    kpi = load_kpi(99115, extra_config)
    spec = {m.key: m for m in kpi.measures}["late_yoy"]
    assert spec.kind == "pct_change"
    assert spec.of == "__late_yoy__of"
    names = {b.name for b in kpi.base_measures}
    assert "__late_yoy__of" in names


def test_unknown_mode_lists_presets(extra_config):
    _write(
        extra_config,
        99120,
        measures={"yoy": {"op": "compare", "of": "sotif_value", "mode": "yoyx"}},
    )
    with pytest.raises(BindError, match="mode") as exc:
        load_kpi(99120, extra_config)
    text = str(exc.value)
    assert "yoy, mom, wow, qoq, pop, diff, pct_change" in text


def test_qoq_at_month_grain_is_bind_error(extra_config):
    _write(
        extra_config,
        99121,
        measures={"chg": {"op": "compare", "of": "sotif_value", "mode": "qoq"}},
    )
    with pytest.raises(BindError, match="qoq") as exc:
        load_kpi(99121, extra_config)
    text = str(exc.value)
    assert "quarter" in text
    assert "pop" in text


def test_mom_at_quarter_grain_is_bind_error(extra_config):
    _write(
        extra_config,
        99122,
        time={
            "column": "event_month",
            "grain": "quarter",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        measures={"chg": {"op": "compare", "of": "sotif_value", "mode": "mom"}},
    )
    with pytest.raises(BindError, match="mom") as exc:
        load_kpi(99122, extra_config)
    assert "month" in str(exc.value)


def test_wow_at_month_grain_is_bind_error(extra_config):
    _write(
        extra_config,
        99123,
        measures={"chg": {"op": "compare", "of": "sotif_value", "mode": "wow"}},
    )
    with pytest.raises(BindError, match="wow") as exc:
        load_kpi(99123, extra_config)
    assert "week" in str(exc.value)


def test_preset_plus_versus_is_bind_error(extra_config):
    _write(
        extra_config,
        99124,
        measures={
            "yoy": {
                "op": "compare",
                "of": "sotif_value",
                "mode": "yoy",
                "versus": {"months": 3},
            }
        },
    )
    with pytest.raises(BindError, match="versus") as exc:
        load_kpi(99124, extra_config)
    text = str(exc.value)
    assert "pct_change" in text or "diff" in text


def test_compare_of_trend_is_bind_error(extra_config):
    _write(
        extra_config,
        99125,
        measures={
            "trend_12m": {
                "of": "sotif_value",
                "op": "trend",
                "trailing": {"months": 12},
                "inclusive": True,
            },
            "yoy": {"op": "compare", "of": "trend_12m", "mode": "yoy"},
        },
    )
    with pytest.raises(BindError, match="cannot shift"):
        load_kpi(99125, extra_config)


def test_compare_on_snapshot_needs_time(extra_config):
    _write(
        extra_config,
        99126,
        time=None,
        measures={"yoy": {"op": "compare", "of": "sotif_value", "mode": "yoy"}},
    )
    with pytest.raises(BindError, match="time"):
        load_kpi(99126, extra_config)


def test_compare_column_is_rejected(extra_config):
    _write(
        extra_config,
        99127,
        measures={
            "yoy": {
                "op": "compare",
                "column": "amount",
                "mode": "yoy",
            }
        },
    )
    with pytest.raises(BindError, match="column"):
        load_kpi(99127, extra_config)


def test_leftover_sugar_kind_is_bind_error():
    from kpi_engine.contracts import OutputSpec
    from kpi_engine.pipeline.binder import _assert_no_sugar_kinds_remain

    leftover = OutputSpec(key="yoy", kind="compare", of="sotif_value")
    with pytest.raises(BindError, match="internal desugar"):
        _assert_no_sugar_kinds_remain((leftover,))
