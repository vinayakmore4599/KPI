"""Post-aggregation joins between per-model extracts.

What this file provides
    Unit tests for relations.join_monthly: join how variants, key coercion,
    chained relations, and every guard that stops a silent wrong join.

Where it is used
    pytest tests/test_relations_unit.py — pandas only, no DuckDB.

When to use
    Add a case when model_relations gains an option (new how, key aliasing).
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine.contracts import BaseMeasure, CutSpec, KpiSpec, ModelRelation, OutputSpec, TimeSpec
from kpi_engine.pipeline.relations import join_monthly
from kpi_engine.exceptions import BindError


def _kpi(bases: tuple[BaseMeasure, ...], relations: tuple[ModelRelation, ...] = ()) -> KpiSpec:
    """KpiSpec carrying only what join_monthly reads."""
    return KpiSpec(
        kpi_id=9400,
        version=1,
        model_id="numerator",
        time=TimeSpec(column="event_month", grain="month", filter_code="reporting_month"),
        dimensions=("region",),
        base_measures=bases,
        cuts=(CutSpec(name="G", group_by=("region",), ignore_filters=(), also_emit=()),),
        default_cut="G",
        measures=(OutputSpec(key="ratio", kind="point", of="num"),),
        model_relations=relations,
    )


def _frame(column: str, rows: list[tuple[date, str, float]]) -> pd.DataFrame:
    """Monthly extract shaped like one model's DuckDB output."""
    return pd.DataFrame(
        [{"event_month": m, "region": r, column: v} for m, r, v in rows]
    )


_BASES = (
    BaseMeasure(name="num", sql="amount", agg="sum", model_id="numerator"),
    BaseMeasure(name="den", sql="target", agg="sum", model_id="denominator"),
)
_REL_KEYS = ("event_month", "region")


def test_no_frames_yields_an_empty_frame():
    """Nothing extracted means nothing to join."""
    assert join_monthly({}, _kpi(_BASES)).empty


def test_single_model_passes_through_untouched():
    """A one-model KPI never merges, so the extract is returned as-is."""
    frame = _frame("num", [(date(2026, 3, 1), "NA", 10.0)])
    kpi = _kpi((BaseMeasure(name="num", sql="amount", agg="sum"),))
    assert join_monthly({"numerator": frame}, kpi) is frame


def test_two_models_without_relations_is_an_error():
    """Two extracts and no declared join is ambiguous, not an implicit cross join."""
    frames = {
        "numerator": _frame("num", [(date(2026, 3, 1), "NA", 10.0)]),
        "denominator": _frame("den", [(date(2026, 3, 1), "NA", 4.0)]),
    }
    with pytest.raises(BindError, match="need model_relations"):
        join_monthly(frames, _kpi(_BASES))


def test_relation_naming_a_model_with_no_extract_is_an_error():
    """A relation must point at models that were actually extracted."""
    frames = {"numerator": _frame("num", [(date(2026, 3, 1), "NA", 10.0)])}
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=_REL_KEYS),))
    with pytest.raises(BindError, match="missing extract"):
        join_monthly(frames, kpi)


def test_join_keys_absent_from_both_frames_are_an_error():
    """A join on columns nobody has would silently produce garbage."""
    frames = {
        "numerator": _frame("num", [(date(2026, 3, 1), "NA", 10.0)]),
        "denominator": _frame("den", [(date(2026, 3, 1), "NA", 4.0)]),
    }
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=("plant_code",)),))
    with pytest.raises(BindError, match="are not in both extracts"):
        join_monthly(frames, kpi)


def test_keys_missing_from_one_side_are_dropped_from_the_join():
    """A global extract without region still joins on the keys it does have."""
    frames = {
        "numerator": _frame("num", [(date(2026, 3, 1), "NA", 10.0)]),
        "denominator": pd.DataFrame([{"event_month": date(2026, 3, 1), "den": 4.0}]),
    }
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=_REL_KEYS),))
    joined = join_monthly(frames, kpi)
    assert joined.loc[0, "num"] == 10.0
    assert joined.loc[0, "den"] == 4.0


@pytest.mark.parametrize(
    ("how", "expected_regions"),
    [
        ("inner", {"NA"}),
        ("left", {"NA", "EU"}),
        ("right", {"NA", "APAC"}),
        ("outer", {"NA", "EU", "APAC"}),
    ],
)
def test_join_how_controls_which_unmatched_keys_survive(how, expected_regions):
    """inner/left/right/outer behave like the SQL joins they are named after."""
    frames = {
        "numerator": _frame(
            "num", [(date(2026, 3, 1), "NA", 10.0), (date(2026, 3, 1), "EU", 5.0)]
        ),
        "denominator": _frame(
            "den", [(date(2026, 3, 1), "NA", 4.0), (date(2026, 3, 1), "APAC", 9.0)]
        ),
    }
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=_REL_KEYS, how=how),))
    joined = join_monthly(frames, kpi)
    assert set(joined["region"]) == expected_regions


def test_time_keys_are_normalized_before_merging():
    """A datetime64 month and a python date month must still match."""
    left = _frame("num", [(date(2026, 3, 1), "NA", 10.0)])
    right = _frame("den", [(date(2026, 3, 1), "NA", 4.0)])
    right["event_month"] = pd.to_datetime(right["event_month"])
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=_REL_KEYS, how="inner"),))
    joined = join_monthly({"numerator": left, "denominator": right}, kpi)
    assert len(joined) == 1
    assert joined.loc[0, "den"] == 4.0


def test_chained_relations_keep_merging_into_one_frame():
    """Three models joined by two relations produce a single row per key."""
    bases = (
        BaseMeasure(name="num", sql="amount", agg="sum", model_id="numerator"),
        BaseMeasure(name="den", sql="target", agg="sum", model_id="denominator"),
        BaseMeasure(name="cap", sql="capacity", agg="sum", model_id="capacity"),
    )
    frames = {
        "numerator": _frame("num", [(date(2026, 3, 1), "NA", 10.0)]),
        "denominator": _frame("den", [(date(2026, 3, 1), "NA", 4.0)]),
        "capacity": _frame("cap", [(date(2026, 3, 1), "NA", 2.0)]),
    }
    kpi = _kpi(
        bases,
        (
            ModelRelation(left="num", right="den", on=_REL_KEYS, how="outer"),
            ModelRelation(left="den", right="cap", on=_REL_KEYS, how="outer"),
        ),
    )
    joined = join_monthly(frames, kpi)
    assert len(joined) == 1
    assert set(["num", "den", "cap"]).issubset(joined.columns)
    assert (joined.loc[0, "num"], joined.loc[0, "den"], joined.loc[0, "cap"]) == (10.0, 4.0, 2.0)


def test_overlapping_value_columns_are_not_duplicated():
    """Columns already on the left are not re-merged with _x/_y suffixes."""
    left = _frame("num", [(date(2026, 3, 1), "NA", 10.0)])
    left["shared_dim"] = "x"
    right = _frame("den", [(date(2026, 3, 1), "NA", 4.0)])
    right["shared_dim"] = "y"
    kpi = _kpi(_BASES, (ModelRelation(left="num", right="den", on=_REL_KEYS, how="inner"),))
    joined = join_monthly({"numerator": left, "denominator": right}, kpi)
    assert list(joined.columns).count("shared_dim") == 1
    assert "shared_dim_x" not in joined.columns
    assert joined.loc[0, "shared_dim"] == "x"
