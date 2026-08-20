"""Cut planner: which grouping grains to emit for a request.

What this file provides
    emitted_cuts — default_cut plus also_emit (e.g. G also emits R).
    cut_group_dims — group_by minus the time column.
    finest_grain — DuckDB GROUP BY keys (time + dimensions ∪ cut keys).

Where it is used
    orchestrator (what to extract and what to calculate). calc_engine
    re-aggregates the monthly frame to each cut.

Capabilities
    G/R (or country, supplier, …) are data in YAML, not hardcoded. Each cut
    is a fresh aggregation; we do not roll avg from R into G.

When to use
    Extend only if cut semantics change (e.g. explicit rollup flag). To add a
    new grain, add a cut in KPI YAML.
"""

from __future__ import annotations

from kpi_engine.contracts import CutSpec, KpiSpec
from kpi_engine.exceptions import BindError


def emitted_cuts(kpi: KpiSpec) -> tuple[CutSpec, ...]:
    """Walk default_cut and also_emit to get the full set of cuts for this request."""
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
        for extra in cut.also_emit:
            walk(extra)

    walk(kpi.default_cut)
    return tuple(by_name[n] for n in names)


def cut_group_dims(cut: CutSpec, time_column: str) -> tuple[str, ...]:
    """Non-time grouping columns for a cut (time is handled on the monthly frame)."""
    return tuple(c for c in cut.group_by if c != time_column)


def finest_grain(kpi: KpiSpec, emitted: tuple[CutSpec, ...]) -> tuple[str, ...]:
    """DuckDB GROUP BY: time column plus the union of dimensions and cut group_bys."""
    dims: list[str] = []
    seen: set[str] = set()
    for name in (kpi.time.column, *kpi.dimensions):
        if name not in seen:
            dims.append(name)
            seen.add(name)
    for cut in emitted:
        for name in cut.group_by:
            if name not in seen:
                dims.append(name)
                seen.add(name)
    return tuple(dims)
