"""Cut planner: which grouping grains to emit for a request.

What this file provides
    emitted_cuts / emitted_cuts_from — default_cut or named roots plus also_emit.
    cut_group_dims — group_by minus the time column.
    finest_grain — union of all KPI dimensions (tests / full-KPI view).
    extract_grain — time + this pipeline's cuts only (what DuckDB retrieves).

Where it is used
    orchestrator (per-extract grain and cuts). calc_engine re-aggregates
    the monthly frame to each cut.

Capabilities
    G/R (or country, supplier, …) are data in YAML, not hardcoded. Each cut
    is a fresh aggregation; we do not roll avg from R into G.

When to use
    Extend only if cut semantics change (e.g. explicit rollup flag). To add a
    new grain, add a cut in KPI YAML.
"""

from __future__ import annotations

from kpi_engine.contracts import BaseMeasure, CutSpec, KpiSpec
from kpi_engine.exceptions import BindError
from kpi_engine.runlog import traced


@traced
def emitted_cuts(kpi: KpiSpec) -> tuple[CutSpec, ...]:
    """Walk default_cut and also_emit to get the full set of cuts for this request."""
    return emitted_cuts_from(kpi, (kpi.default_cut,))


def emitted_cuts_from(kpi: KpiSpec, roots: tuple[str, ...]) -> tuple[CutSpec, ...]:
    """Walk these cut names and also_emit. Used per extract pipeline."""
    by_name = {c.name: c for c in kpi.cuts}
    if kpi.locked_cut is not None:
        cut = by_name.get(kpi.locked_cut)
        if cut is None:
            raise BindError(
                f"Unknown cut {kpi.locked_cut!r}. Declared: {sorted(by_name)}."
            )
        return (cut,)
    names: list[str] = []

    def walk(name: str) -> None:
        """Depth-first include this cut then any cuts it asks to also_emit."""
        if name in names:
            return
        cut = by_name.get(name)
        if cut is None:
            raise BindError(f"Unknown cut {name!r}. Declared: {sorted(by_name)}.")
        names.append(name)
        for extra in cut.also_emit:
            walk(extra)

    for root in roots:
        walk(str(root))
    return tuple(by_name[n] for n in names)


def cut_group_dims(cut: CutSpec, time_column: str) -> tuple[str, ...]:
    """Non-time grouping columns for a cut (time is handled on the monthly frame)."""
    return tuple(c for c in cut.group_by if c != time_column)


@traced
def finest_grain(kpi: KpiSpec, emitted: tuple[CutSpec, ...]) -> tuple[str, ...]:
    """Columns DuckDB must return: time, dimensions, cut keys, and join keys."""
    dims: list[str] = []
    seen: set[str] = set()
    time_names = (kpi.time.column,) if kpi.time is not None else ()
    dim_sources = tuple(spec.source or spec.name for spec in kpi.dimension_specs) or kpi.dimensions
    rename_to_source = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}
    extra = tuple(m.where.column for m in kpi.base_measures if m.where is not None)
    for name in (*time_names, *dim_sources, *extra):
        if name not in seen:
            dims.append(name)
            seen.add(name)
    for cut in emitted:
        for name in cut.group_by:
            physical = rename_to_source.get(name, name)
            if physical not in seen:
                dims.append(physical)
                seen.add(physical)
            seen.add(name)
    for rel in kpi.model_relations:
        for name in rel.on:
            if name not in seen:
                dims.append(name)
                seen.add(name)
    return tuple(dims)


def extract_grain(
    kpi: KpiSpec,
    emitted: tuple[CutSpec, ...],
    bases: tuple[BaseMeasure, ...] = (),
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Time + these cuts' group_by + where columns + extras. Not every KPI dimension."""
    names: list[str] = []
    seen: set[str] = set()
    rename = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}

    def add(name: str | None) -> None:
        if not name or name in seen:
            return
        names.append(name)
        seen.add(name)

    if kpi.time is not None:
        add(kpi.time.column)
    for cut in emitted:
        for name in cut.group_by:
            add(rename.get(name, name))
    for base in bases:
        if base.where is not None:
            add(base.where.column)
    for name in extra:
        add(name)
    return tuple(names)
