"""Bind remaining filters to columns. Default operator is IN."""

from __future__ import annotations

from kpi_engine.contracts import BoundFilter, CutSpec, DatasetBinding, IncomingFilter, KpiSpec
from kpi_engine.exceptions import FilterError


def bind_filters(
    remaining: tuple[IncomingFilter, ...],
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    extract_columns: set[str],
) -> tuple[BoundFilter, ...]:
    mappings = _mapping_index(datasets, kpi)
    bound: list[BoundFilter] = []
    for item in remaining:
        column = _resolve_column(item, mappings, kpi, extract_columns)
        stage: str = "source" if column in extract_columns else "target"
        if stage == "target":
            raise FilterError(
                f"Filter {item.raw_key!r} does not bind to a source column. "
                "Unmapped filters are a hard error."
            )
        bound.append(
            BoundFilter(
                code=item.code,
                column=column,
                values=item.values,
                stage="source",
                input_text=item.input_text,
            )
        )
    return tuple(bound)


def split_for_duckdb(
    bound: tuple[BoundFilter, ...], emitted: tuple[CutSpec, ...]
) -> tuple[tuple[BoundFilter, ...], tuple[BoundFilter, ...]]:
    """Filters ignored by any emitted cut stay out of DuckDB so G can see all regions."""
    ignored = {code for cut in emitted for code in _ignore_names(cut)}
    source: list[BoundFilter] = []
    deferred: list[BoundFilter] = []
    for item in bound:
        if item.code in ignored or item.column in ignored or _norm(item.code) in ignored:
            deferred.append(item)
        else:
            source.append(item)
    return tuple(source), tuple(deferred)


def apply_cut_filters(frame, cut: CutSpec, deferred: tuple[BoundFilter, ...]):
    work = frame
    for item in deferred:
        if _is_ignored(cut, item):
            continue
        if not item.values:
            return work.iloc[0:0].copy()
        work = work[work[item.column].isin(list(item.values))]
    return work


def _is_ignored(cut: CutSpec, item: BoundFilter) -> bool:
    names = _ignore_names(cut)
    return item.code in names or item.column in names or _norm(item.code) in names


def _ignore_names(cut: CutSpec) -> set[str]:
    names: set[str] = set()
    for raw in cut.ignore_filters:
        names.add(raw)
        names.add(_norm(raw))
    return names


def _resolve_column(
    item: IncomingFilter,
    mappings: dict[str, str],
    kpi: KpiSpec,
    extract_columns: set[str],
) -> str:
    mapped = mappings.get(_norm(item.code)) or mappings.get(_norm(item.raw_key))
    if mapped:
        return mapped
    yaml_map = {k.lower(): v for k, v in kpi.filter_map.items()}
    if _norm(item.code) in yaml_map:
        return yaml_map[_norm(item.code)]
    if item.raw_key in extract_columns:
        return item.raw_key
    if item.code in extract_columns:
        return item.code
    raise FilterError(
        f"Filter {item.raw_key!r} has no column mapping and is not a source column."
    )


def _mapping_index(
    datasets: dict[str, DatasetBinding], kpi: KpiSpec
) -> dict[str, str]:
    index: dict[str, str] = {}
    for dataset in datasets.values():
        for mapping in dataset.mappings:
            index[_norm(mapping.filter_code)] = mapping.column_name
    for key, column in kpi.filter_map.items():
        index[_norm(key)] = column
    return index


def _norm(value: str) -> str:
    return value.strip().lower().replace(" ", "_")
