"""Cut planner: which grouping grains to emit for a request.

What this file provides
    emitted_cuts / emitted_cuts_from — default_cut or named roots plus also_emit.
    effective_group_by — request grain minus exclude_from_grain plus cut extras.
    cut_group_dims — effective keys minus the time column.
    extract_grain / finest_grain — time + this pipeline's effective cut keys.

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
from kpi_engine.identifiers import norm_name
from kpi_engine.runlog import traced


@traced
def emitted_cuts(kpi: KpiSpec) -> tuple[CutSpec, ...]:
    """Walk default_cut (or locked output_cut) and also_emit."""
    root = kpi.locked_cut or kpi.default_cut
    return emitted_cuts_from(kpi, (root,))


def emitted_cuts_from(kpi: KpiSpec, roots: tuple[str, ...]) -> tuple[CutSpec, ...]:
    """Walk these cut names and also_emit. Used per extract pipeline.

    A cut with pack_also_emit false is emitted without walking its also_emit.
    parameters.output_cut (locked_cut) is a walk root, not a hard lock.
    """
    by_name = {c.name: c for c in kpi.cuts}
    names: list[str] = []

    def walk(name: str) -> None:
        """Depth-first include this cut then any cuts it asks to also_emit."""
        if name in names:
            return
        cut = by_name.get(name)
        if cut is None:
            raise BindError(f"Unknown cut {name!r}. Declared: {sorted(by_name)}.")
        names.append(name)
        if not cut.pack_also_emit:
            return
        for extra in cut.also_emit:
            walk(extra)

    for root in roots:
        walk(str(root))
    return tuple(by_name[n] for n in names)


def effective_group_by(cut: CutSpec, kpi: KpiSpec | None) -> tuple[str, ...]:
    """Request grain minus this cut's exclude list, then YAML extras.

    ``CutSpec.group_by`` is extras only. Without a KPI (unit tests of extras
    themselves) the extras tuple is returned unchanged.
    """
    if kpi is None:
        return cut.group_by
    grain = kpi.request_grain
    exclude = {norm_name(name) for name in cut.exclude_from_grain}
    names: list[str] = []
    seen: set[str] = set()
    for name in grain:
        key = norm_name(name)
        if key in exclude or key in seen:
            continue
        names.append(name)
        seen.add(key)
    for name in cut.group_by:
        key = norm_name(name)
        if key in seen:
            continue
        names.append(name)
        seen.add(key)
    return tuple(names)


def cut_group_dims(
    cut: CutSpec, time_column: str, kpi: KpiSpec | None = None
) -> tuple[str, ...]:
    """Non-time grouping columns for a cut (time is handled on the monthly frame)."""
    grouping = effective_group_by(cut, kpi)
    return tuple(c for c in grouping if c != time_column)


@traced
def finest_grain(kpi: KpiSpec, emitted: tuple[CutSpec, ...]) -> tuple[str, ...]:
    """Alias of extract_grain: time plus effective keys of these cuts."""
    return extract_grain(kpi, emitted)


def extract_grain(
    kpi: KpiSpec,
    emitted: tuple[CutSpec, ...],
    bases: tuple[BaseMeasure, ...] = (),
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Time + these cuts' effective group_by + where columns + extras."""
    names: list[str] = []
    seen: set[str] = set()
    rename = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}
    by_fold = {
        norm_name(spec.name): spec.source or spec.name
        for spec in kpi.dimension_specs
    }
    by_fold.update(
        {
            norm_name(spec.source): spec.source or spec.name
            for spec in kpi.dimension_specs
            if spec.source
        }
    )

    def add(name: str | None) -> None:
        if not name:
            return
        physical = rename.get(name, by_fold.get(norm_name(name), name))
        key = norm_name(physical)
        if key in seen:
            return
        names.append(physical)
        seen.add(key)

    if kpi.time is not None:
        add(kpi.time.column)
    for cut in emitted:
        for name in effective_group_by(cut, kpi):
            add(rename.get(name, name))
    for base in bases:
        if base.where is not None:
            add(base.where.column)
        for extra_where in base.also_where:
            add(extra_where.column)
    for name in extra:
        add(rename.get(name, name))
    return tuple(names)
