"""Calculation semantics on sparse data: nulls, zero-fill, and op guards.

What this file provides
    How each op behaves when a period has no rows — point vs window vs trend,
    per aggregation — plus arithmetic null propagation and catalog errors.

Where it is used
    pytest tests/test_calc_semantics.py.

When to use
    Add a case whenever "what does this measure return when data is missing"
    could be answered two ways. That question belongs here, in one place.
"""

import json
from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.contracts import OutputSpec, TimePlan
from kpi_engine.core.binder import load_kpi
from kpi_engine.core.calc_engine import densify, evaluate
from kpi_engine.exceptions import CatalogError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml

_BASES = {
    "sum_value": {"sql": "amount", "agg": "sum"},
    "min_value": {"sql": "amount", "agg": "min"},
    "max_value": {"sql": "amount", "agg": "max"},
    "avg_value": {"sql": "amount", "agg": "avg"},
    "count_value": {"sql": "amount", "agg": "count"},
}


@pytest.fixture
def gapped_parquet(tmp_path):
    """January and March 2026 only: February is a real hole in the data."""
    rows = [
        {
            "event_month": month,
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": amount,
        }
        for month, amount in ((date(2026, 1, 1), 10.0), (date(2026, 1, 1), 30.0), (date(2026, 3, 1), 8.0))
    ]
    path = tmp_path / "gapped.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _sparse_kpi(kpi_id: int) -> dict:
    """KPI exposing every additive agg as point, window, and trend measures."""
    measures: dict[str, dict] = {}
    for base in _BASES:
        measures[f"{base}_now"] = {"of": base, "op": "point", "offset": {"months": 0}}
        measures[f"{base}_prev"] = {"of": base, "op": "point", "offset": {"months": 1}}
        measures[f"{base}_3m"] = {
            "of": base,
            "op": "window",
            "trailing": {"months": 3},
            "inclusive": True,
        }
        measures[f"{base}_trend"] = {
            "of": base,
            "op": "trend",
            "trailing": {"months": 3},
            "inclusive": True,
        }
    return minimal_kpi(
        kpi_id,
        base_measures=_BASES,
        cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
        default_cut="G",
        measures=measures,
    )


@pytest.fixture
def sparse_row(gapped_parquet, extra_config):
    """The single G row for a KPI covering every agg over gapped data."""
    write_yaml(extra_config / "kpis" / "9600.yaml", _sparse_kpi(9600))
    keys = [k for k in _sparse_kpi(9600)["measures"]]
    ctx = make_context(
        gapped_parquet, measures=keys, supplier=["ABC"], kpi_id=9600, month="2026-03"
    )
    result = compute(ctx, config_dir=extra_config)
    assert len(result["rows"]) == 1
    return result["rows"][0]


def test_point_on_a_month_with_no_rows_is_null_for_every_agg(sparse_row):
    """February has no rows, so no aggregation can invent a value for it."""
    for base in _BASES:
        assert sparse_row[f"{base}_prev"] is None, base


def test_point_on_a_month_with_rows_uses_the_declared_agg(sparse_row):
    """March holds a single 8.0 row."""
    assert sparse_row["sum_value_now"] == 8.0
    assert sparse_row["min_value_now"] == 8.0
    assert sparse_row["max_value_now"] == 8.0
    assert sparse_row["avg_value_now"] == 8.0
    assert sparse_row["count_value_now"] == 1.0


def test_windows_skip_the_gap_rather_than_treating_it_as_zero(sparse_row):
    """A missing month must not drag min down to 0 or avg toward it."""
    assert sparse_row["sum_value_3m"] == 48.0
    assert sparse_row["min_value_3m"] == 8.0
    assert sparse_row["max_value_3m"] == 30.0
    assert sparse_row["count_value_3m"] == 3.0
    assert sparse_row["avg_value_3m"] == pytest.approx(16.0)


def test_trend_zero_fills_additive_aggs_and_nulls_the_rest(sparse_row):
    """A graph line stays continuous for sums and counts, and breaks for min/max/avg."""
    assert sparse_row["sum_value_trend"] == [40.0, 0.0, 8.0]
    assert sparse_row["count_value_trend"] == [2.0, 0.0, 1.0]
    assert sparse_row["min_value_trend"] == [10.0, None, 8.0]
    assert sparse_row["max_value_trend"] == [30.0, None, 8.0]
    assert sparse_row["avg_value_trend"] == [20.0, None, 8.0]


def test_trend_axis_is_shared_and_matches_value_length(gapped_parquet, extra_config):
    """Every trend of the same length reuses one axis in response metadata."""
    write_yaml(extra_config / "kpis" / "9601.yaml", _sparse_kpi(9601))
    ctx = make_context(
        gapped_parquet,
        measures=["sum_value_trend", "min_value_trend"],
        supplier=["ABC"],
        kpi_id=9601,
    )
    result = compute(ctx, config_dir=extra_config)
    axes = result["trend_axes"]
    assert axes["sum_value_trend"] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert axes["min_value_trend"] == axes["sum_value_trend"]
    assert len(result["rows"][0]["sum_value_trend"]) == len(axes["sum_value_trend"])


def test_window_over_an_empty_span_is_zero_for_sums_and_null_otherwise(
    gapped_parquet, extra_config
):
    """Sums treat an empty span as zero (the spine is zero-filled); min/max stay null."""
    spec = minimal_kpi(
        9602,
        base_measures={
            "sum_value": {"sql": "amount", "agg": "sum"},
            "min_value": {"sql": "amount", "agg": "min"},
        },
        cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
        default_cut="G",
        measures={
            "prior_sum": {
                "of": "sum_value",
                "op": "window",
                "trailing": {"months": 2},
                "inclusive": False,
            },
            "prior_min": {
                "of": "min_value",
                "op": "window",
                "trailing": {"months": 2},
                "inclusive": False,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9602.yaml", spec)
    ctx = make_context(
        gapped_parquet,
        measures=["prior_sum", "prior_min"],
        supplier=["ABC"],
        kpi_id=9602,
        month="2026-01",
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["prior_sum"] == 0.0
    assert row["prior_min"] is None


def test_coarser_cuts_recompute_min_and_max_instead_of_adding_them(tmp_path, extra_config):
    """A global cut takes the min across regions, never the sum of regional minima."""
    rows = [
        {
            "event_month": date(2026, 3, 1),
            "region": region,
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": amount,
        }
        for region, amount in (("NA", 5.0), ("EU", 7.0))
    ]
    path = tmp_path / "two_regions.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9603.yaml",
        minimal_kpi(
            9603,
            base_measures={
                "min_value": {"sql": "amount", "agg": "min"},
                "max_value": {"sql": "amount", "agg": "max"},
                "sum_value": {"sql": "amount", "agg": "sum"},
            },
            measures={
                "min_now": {"of": "min_value", "op": "point", "offset": {"months": 0}},
                "max_now": {"of": "max_value", "op": "point", "offset": {"months": 0}},
                "sum_now": {"of": "sum_value", "op": "point", "offset": {"months": 0}},
            },
        ),
    )
    ctx = make_context(
        path, measures=["min_now", "max_now", "sum_now"], supplier=["ABC"], kpi_id=9603
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert (g["min_now"], g["max_now"]) == (5.0, 7.0)
    assert g["sum_now"] == 12.0
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    assert (na["min_now"], eu["min_now"]) == (5.0, 7.0)


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        ("add", 56.0),
        ("sub", 40.0),
        ("mul", 384.0),
        ("div", 6.0),
        ("percent", 600.0),
        ("growth_pct", 5.0),
        ("yoy", 5.0),
        ("mom", 5.0),
    ],
)
def test_arithmetic_functions_combine_two_measures(gapped_parquet, extra_config, fn, expected):
    """Every catalog arithmetic op is exercised against the same two inputs."""
    spec = _arithmetic_kpi(9610, fn)
    write_yaml(extra_config / "kpis" / "9610.yaml", spec)
    ctx = make_context(
        gapped_parquet, measures=["combined"], supplier=["ABC"], kpi_id=9610, month="2026-03"
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["combined"] == pytest.approx(expected)


@pytest.mark.parametrize("fn", ["add", "sub", "mul", "div", "percent", "growth_pct"])
def test_arithmetic_propagates_nulls_instead_of_guessing(gapped_parquet, extra_config, fn):
    """If either side is null (February has no rows), the result is null."""
    spec = _arithmetic_kpi(9611, fn, left_offset=1)
    write_yaml(extra_config / "kpis" / "9611.yaml", spec)
    ctx = make_context(
        gapped_parquet, measures=["combined"], supplier=["ABC"], kpi_id=9611, month="2026-03"
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["combined"] is None


def test_dimension_measures_are_returned_as_columns(parquet_path, config_dir):
    """A measure_key of kind dimension projects the dimension value, not a number."""
    ctx = make_context(
        parquet_path, measures=["reason_code", "current_value"], supplier=["ABC"]
    )
    rows = compute(ctx, config_dir=config_dir)["rows"]
    assert {row["reason_code"] for row in rows} == {"LATE_SUPPLIER", "OTHER"}


def test_measure_pointing_at_an_unknown_base_fails(gapped_parquet, extra_config):
    """`of:` must name a declared base measure."""
    spec = minimal_kpi(
        9612,
        measures={"current_value": {"of": "ghost_value", "op": "point", "offset": {"months": 0}}},
    )
    write_yaml(extra_config / "kpis" / "9612.yaml", spec)
    ctx = make_context(gapped_parquet, measures=["current_value"], supplier=["ABC"], kpi_id=9612)
    with pytest.raises(CatalogError, match="Unknown base measure 'ghost_value'"):
        compute(ctx, config_dir=extra_config)


def test_window_without_of_fails(gapped_parquet, extra_config):
    """A window needs to know which fact it aggregates."""
    spec = minimal_kpi(
        9613,
        measures={"value_3m": {"op": "window", "trailing": {"months": 3}, "inclusive": True}},
    )
    write_yaml(extra_config / "kpis" / "9613.yaml", spec)
    ctx = make_context(gapped_parquet, measures=["value_3m"], supplier=["ABC"], kpi_id=9613)
    with pytest.raises(CatalogError, match="requires `of`"):
        compute(ctx, config_dir=extra_config)


def test_evaluate_rejects_an_unknown_op(config_dir):
    """The dispatcher fails loudly rather than returning None for an unknown kind."""
    kpi = load_kpi(3004, config_dir)
    plan = TimePlan(
        anchor=date(2026, 3, 1),
        span_start=date(2026, 3, 1),
        span_end_exclusive=date(2026, 4, 1),
        lookback_months=0,
        claimed_filter_code="reporting_month",
    )
    with pytest.raises(CatalogError, match="Cannot evaluate"):
        evaluate(OutputSpec(key="mystery", kind="teleport"), pd.DataFrame(), kpi, plan, {})


def test_densify_fills_every_partition_and_period():
    """The spine is the cross product of partitions and periods in the span."""
    frame = pd.DataFrame(
        [
            {"region": "NA", "event_month": date(2026, 1, 1), "sotif_value": 10.0},
            {"region": "EU", "event_month": date(2026, 3, 1), "sotif_value": 5.0},
        ]
    )
    dense = densify(
        frame,
        keys=["region"],
        time_col="event_month",
        start=date(2026, 1, 1),
        end=date(2026, 3, 1),
        value_cols=["sotif_value"],
        fill_zero_cols=["sotif_value"],
    )
    assert len(dense) == 6
    assert set(dense["region"]) == {"NA", "EU"}
    assert dense["_observed"].sum() == 2
    assert dense["sotif_value"].isna().sum() == 0
    na_feb = dense[(dense["region"] == "NA") & (dense["event_month"] == pd.Timestamp("2026-02-01"))]
    assert na_feb.iloc[0]["sotif_value"] == 0.0
    assert bool(na_feb.iloc[0]["_observed"]) is False


def test_densify_without_partition_keys_still_builds_the_period_spine():
    """A KPI with no dimensions gets one row per period."""
    frame = pd.DataFrame([{"event_month": date(2026, 2, 1), "sotif_value": 7.0}])
    dense = densify(
        frame,
        keys=[],
        time_col="event_month",
        start=date(2026, 1, 1),
        end=date(2026, 3, 1),
        value_cols=["sotif_value"],
        fill_zero_cols=[],
    )
    assert list(dense["event_month"]) == [
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-02-01"),
        pd.Timestamp("2026-03-01"),
    ]
    assert list(dense["_observed"]) == [False, True, False]


def test_nullable_fact_column_yields_null_averages_not_zero(tmp_path, extra_config):
    """A month whose measure column is entirely NULL has no average, even though rows exist."""
    rows = [
        {
            "event_month": date(2026, 2, 1),
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": 10.0,
            "bonus": None,
        },
        {
            "event_month": date(2026, 3, 1),
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "amount": 20.0,
            "bonus": 4.0,
        },
    ]
    path = tmp_path / "nullable.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9614.yaml",
        minimal_kpi(
            9614,
            base_measures={
                "amount_value": {"sql": "amount", "agg": "sum"},
                "bonus_avg": {"sql": "bonus", "agg": "avg"},
            },
            cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
            default_cut="G",
            measures={
                "amount_prev": {"of": "amount_value", "op": "point", "offset": {"months": 1}},
                "bonus_prev": {"of": "bonus_avg", "op": "point", "offset": {"months": 1}},
                "bonus_now": {"of": "bonus_avg", "op": "point", "offset": {"months": 0}},
                "bonus_prior_1m": {
                    "of": "bonus_avg",
                    "op": "window",
                    "trailing": {"months": 1},
                    "inclusive": False,
                },
                "bonus_trend": {
                    "of": "bonus_avg",
                    "op": "trend",
                    "trailing": {"months": 2},
                    "inclusive": True,
                    "cuts": ["G"],
                },
            },
        ),
    )
    ctx = make_context(
        path,
        measures=["amount_prev", "bonus_prev", "bonus_now", "bonus_prior_1m", "bonus_trend"],
        supplier=["ABC"],
        kpi_id=9614,
        month="2026-03",
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["amount_prev"] == 10.0
    assert row["bonus_prev"] is None
    assert row["bonus_prior_1m"] is None
    assert row["bonus_now"] == 4.0
    assert row["bonus_trend"] == [None, 4.0]


def test_trend_of_a_non_additive_agg_recomputes_each_period(tmp_path, extra_config):
    """A distinct count per month cannot be summed, so each slot is recomputed."""
    rows = [
        {
            "event_month": month,
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": supplier,
            "amount": 1.0,
        }
        for month, supplier in (
            (date(2026, 1, 1), "ABC"),
            (date(2026, 1, 1), "XYZ"),
            (date(2026, 3, 1), "ABC"),
        )
    ]
    path = tmp_path / "distinct_trend.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9617.yaml",
        minimal_kpi(
            9617,
            base_measures={"distinct_suppliers": {"sql": "supplier_name", "agg": "count_distinct"}},
            cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
            default_cut="G",
            measures={
                "suppliers_trend": {
                    "of": "distinct_suppliers",
                    "op": "trend",
                    "trailing": {"months": 3},
                    "inclusive": True,
                },
                "suppliers_3m": {
                    "of": "distinct_suppliers",
                    "op": "window",
                    "trailing": {"months": 3},
                    "inclusive": True,
                },
            },
        ),
    )
    ctx = make_context(
        path,
        measures=["suppliers_trend", "suppliers_3m"],
        supplier=["ABC", "XYZ"],
        kpi_id=9617,
        month="2026-03",
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["suppliers_trend"] == [2.0, None, 1.0]
    assert row["suppliers_3m"] == 2.0


def test_numeric_and_null_dimensions_serialize_as_plain_json(tmp_path, extra_config):
    """Dimension values reach JSON as python scalars, and missing keys as null."""
    rows = [
        {
            "event_month": date(2026, 3, 1),
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "plant_id": 42,
            "amount": 10.0,
        },
        {
            "event_month": date(2026, 3, 1),
            "region": "NA",
            "reason_code": "LATE_SUPPLIER",
            "supplier_name": "ABC",
            "plant_id": None,
            "amount": 5.0,
        },
    ]
    path = tmp_path / "plants.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9615.yaml",
        minimal_kpi(
            9615,
            dimensions=[{"name": "plant_id", "kind": "dimension"}],
            cuts=[{"name": "P", "group_by": ["plant_id"], "ignore_filters": []}],
            default_cut="P",
            measures={"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
        ),
    )
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9615)
    ctx["datasets"]["Sotif"]["columns"] = [*ctx["datasets"]["Sotif"]["columns"], "plant_id"]
    result = compute(ctx, config_dir=extra_config)
    json.dumps(result, allow_nan=False)
    values = [row["plant_id"] for row in result["rows"]]
    assert sorted(v for v in values if v is not None) == [42]
    assert None in values
    assert all(v is None or type(v) in (int, float) for v in values), values


def test_non_additive_measure_on_an_empty_period_is_null(parquet_path, extra_config):
    """count_distinct has no rows to count before the data starts, so it is null."""
    write_yaml(
        extra_config / "kpis" / "9616.yaml",
        minimal_kpi(
            9616,
            base_measures={"distinct_suppliers": {"sql": "supplier_name", "agg": "count_distinct"}},
            cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
            default_cut="G",
            measures={
                "suppliers_now": {
                    "of": "distinct_suppliers",
                    "op": "point",
                    "offset": {"months": 0},
                },
                "suppliers_prev_year": {
                    "of": "distinct_suppliers",
                    "op": "point",
                    "offset": {"years": 1},
                },
            },
        ),
    )
    ctx = make_context(
        parquet_path,
        measures=["suppliers_now", "suppliers_prev_year"],
        supplier=["ABC"],
        kpi_id=9616,
        month="2025-01",
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["suppliers_now"] == 1.0
    assert row["suppliers_prev_year"] is None


def _arithmetic_kpi(kpi_id: int, fn: str, left_offset: int = 0) -> dict:
    """KPI whose `combined` measure applies fn to March (40 in Jan, 8 in Mar) inputs."""
    return minimal_kpi(
        kpi_id,
        base_measures={"sum_value": {"sql": "amount", "agg": "sum"}},
        cuts=[{"name": "G", "group_by": ["reason_code"], "ignore_filters": []}],
        default_cut="G",
        measures={
            "left_value": {
                "of": "sum_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            }
            if left_offset == 0
            else {"of": "sum_value", "op": "point", "offset": {"months": left_offset}},
            "right_value": {"of": "sum_value", "op": "point", "offset": {"months": 0}},
            "combined": {
                "op": "arithmetic",
                "fn": fn,
                "left": "left_value",
                "right": "right_value",
            },
        },
    )
