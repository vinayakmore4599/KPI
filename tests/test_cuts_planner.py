"""Cut planning: which grains are emitted and what DuckDB must group by.

What this file provides
    Unit tests for emitted_cuts, cut_group_dims, and finest_grain — the layer
    that turns YAML cuts into one extract plus several output grains.

Where it is used
    pytest tests/test_cuts_planner.py — no DuckDB.

When to use
    Add a case when cut semantics change (new rollup flag, nested also_emit).
"""

import pytest

from kpi_engine.contracts import BaseMeasure, CutSpec, KpiSpec, ModelRelation, OutputSpec, TimeSpec
from kpi_engine.core.cuts import cut_group_dims, emitted_cuts, extract_grain, finest_grain
from kpi_engine.exceptions import BindError


def _kpi(cuts: tuple[CutSpec, ...], default: str, **overrides) -> KpiSpec:
    """KpiSpec carrying only what the cut planner reads."""
    spec = dict(
        kpi_id=9300,
        version=1,
        model_id="sotif",
        time=TimeSpec(column="event_month", grain="month", filter_code="reporting_month"),
        dimensions=("reason_code", "region"),
        base_measures=(BaseMeasure(name="sotif_value", sql="amount", agg="sum"),),
        cuts=cuts,
        default_cut=default,
        measures=(OutputSpec(key="current_value", kind="point", of="sotif_value"),),
    )
    spec.update(overrides)
    return KpiSpec(**spec)


def test_locked_cut_skips_also_emit():
    """parameters.output_cut emits that cut only, even when also_emit is set."""
    cuts = (
        CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=("R",)),
        CutSpec(name="R", group_by=("reason_code", "region"), ignore_filters=(), also_emit=()),
    )
    assert [c.name for c in emitted_cuts(_kpi(cuts, "G", locked_cut="G"))] == ["G"]
    assert [c.name for c in emitted_cuts(_kpi(cuts, "G", locked_cut="R"))] == ["R"]
    """A KPI that declares extra cuts still emits just the default unless asked."""
    cuts = (
        CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=()),
        CutSpec(name="R", group_by=("reason_code", "region"), ignore_filters=(), also_emit=()),
    )
    assert [c.name for c in emitted_cuts(_kpi(cuts, "G"))] == ["G"]


def test_also_emit_chains_depth_first_and_keeps_the_default_first():
    """G → R → S emits all three, default first, in declaration order."""
    cuts = (
        CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=("R",)),
        CutSpec(name="R", group_by=("region",), ignore_filters=(), also_emit=("S",)),
        CutSpec(name="S", group_by=("supplier_name",), ignore_filters=(), also_emit=()),
    )
    assert [c.name for c in emitted_cuts(_kpi(cuts, "G"))] == ["G", "R", "S"]


def test_also_emit_cycles_terminate_without_duplicates():
    """Mutually referencing cuts are emitted once each, not forever."""
    cuts = (
        CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=("R",)),
        CutSpec(name="R", group_by=("region",), ignore_filters=(), also_emit=("G",)),
    )
    assert [c.name for c in emitted_cuts(_kpi(cuts, "G"))] == ["G", "R"]


def test_unknown_cut_names_are_reported_with_the_declared_list():
    """A typo in default_cut or also_emit fails with the valid names."""
    cuts = (CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=("Typo",)),)
    with pytest.raises(BindError, match="Unknown cut 'Typo'. Declared: \\['G'\\]"):
        emitted_cuts(_kpi(cuts, "G"))


def test_cut_group_dims_drops_the_time_column():
    """Time is handled by the monthly spine, never as a cut dimension."""
    cut = CutSpec(
        name="T",
        group_by=("event_month", "reason_code"),
        ignore_filters=(),
        also_emit=(),
    )
    assert cut_group_dims(cut, "event_month") == ("reason_code",)
    assert cut_group_dims(cut, "other_column") == ("event_month", "reason_code")


def test_finest_grain_is_time_plus_emitted_cut_keys():
    """One extract serves every emitted cut; catalog dims that no cut uses stay out."""
    cuts = (
        CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=("S",)),
        CutSpec(
            name="S",
            group_by=("reason_code", "supplier_name"),
            ignore_filters=(),
            also_emit=(),
        ),
    )
    kpi = _kpi(cuts, "G")
    grain = finest_grain(kpi, emitted_cuts(kpi))
    assert grain == ("event_month", "reason_code", "supplier_name")
    assert "region" not in grain


def test_finest_grain_does_not_auto_include_join_keys():
    """Join keys are extract extras from the orchestrator, not a catalog union."""
    cuts = (CutSpec(name="G", group_by=("reason_code",), ignore_filters=(), also_emit=()),)
    kpi = _kpi(
        cuts,
        "G",
        base_measures=(
            BaseMeasure(name="num", sql="amount", agg="sum"),
            BaseMeasure(name="den", sql="amount", agg="sum", model_id="other"),
        ),
        model_relations=(
            ModelRelation(left="num", right="den", on=("event_month", "plant_code")),
        ),
    )
    assert finest_grain(kpi, emitted_cuts(kpi)) == ("event_month", "reason_code")
    assert extract_grain(kpi, emitted_cuts(kpi), extra=("plant_code",)) == (
        "event_month",
        "reason_code",
        "plant_code",
    )


def test_finest_grain_does_not_repeat_columns():
    """A dimension that is also a cut key and a join key appears once."""
    cuts = (
        CutSpec(
            name="G",
            group_by=("event_month", "region", "reason_code"),
            ignore_filters=(),
            also_emit=(),
        ),
    )
    kpi = _kpi(
        cuts,
        "G",
        base_measures=(
            BaseMeasure(name="num", sql="amount", agg="sum"),
            BaseMeasure(name="den", sql="amount", agg="sum", model_id="other"),
        ),
        model_relations=(ModelRelation(left="num", right="den", on=("region",)),),
    )
    grain = finest_grain(kpi, emitted_cuts(kpi))
    assert grain == ("event_month", "region", "reason_code")
    assert len(grain) == len(set(grain))


def test_extract_grain_is_time_plus_these_cuts_only():
    """A pipeline does not pull every KPI dimension — only the cuts it will emit."""
    cuts = (
        CutSpec(name="G156", group_by=("region",), ignore_filters=(), also_emit=()),
        CutSpec(
            name="G157",
            group_by=("reason_code", "factor"),
            ignore_filters=(),
            also_emit=(),
        ),
    )
    kpi = _kpi(cuts, "G156", dimensions=("region", "reason_code", "factor"))
    grain = extract_grain(kpi, (cuts[1],))
    assert grain == ("event_month", "reason_code", "factor")
    assert "region" not in grain
