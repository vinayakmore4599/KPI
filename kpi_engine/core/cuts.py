"""Plan which cuts to emit from default_cut + also_emit."""

from __future__ import annotations

from kpi_engine.contracts import CutSpec, KpiSpec
from kpi_engine.exceptions import BindError


def emitted_cuts(kpi: KpiSpec) -> tuple[CutSpec, ...]:
    by_name = {c.name: c for c in kpi.cuts}
    names: list[str] = []

    def walk(name: str) -> None:
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
    return tuple(c for c in cut.group_by if c != time_column)


def finest_grain(kpi: KpiSpec, emitted: tuple[CutSpec, ...]) -> tuple[str, ...]:
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
