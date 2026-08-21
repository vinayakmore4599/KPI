"""Bind remaining filters to columns. Default operator is IN.

What this file provides
    bind_filters — map filter_code → column (context mappings, then YAML filter_map).
    split_for_duckdb — source IN vs deferred (ignored by any emitted cut).
    apply_cut_filters — Pandas isin for deferred filters per cut.

Where it is used
    orchestrator after plan_time. calc_engine.apply path for G vs R region.

Capabilities
    Unmapped filters fail hard. Empty IN list matches nothing (FALSE), not
    invalid SQL. Filters listed in a cut's ignore_filters stay out of DuckDB
    so global cuts can still see all regions. On SQL/CTE models, only columns
    declared in output_schema (the model SELECT) can be IN-filtered in DuckDB.

When to use
    Change mapping rules if metadata mappings change. Hierarchy (heir) is
    rejected in the adapter, not here.
"""

from __future__ import annotations

from kpi_engine.contracts import BoundFilter, CutSpec, DatasetBinding, IncomingFilter, KpiSpec, ModelSpec
from kpi_engine.exceptions import FilterError
from kpi_engine.runlog import traced


@traced
def columns_for_source_filters(
    model: ModelSpec,
    kpi: KpiSpec,
    grain: tuple[str, ...],
    datasets: dict[str, DatasetBinding],
) -> set[str]:
    """Columns DuckDB may IN-filter: model SELECT (output_schema) or physical table columns.

    SQL models declare their SELECT as output_schema. Dim columns that exist only
    inside a CTE (eligible, weight, …) are not filterable on the wrapped query.
    Physical models expose every context dataset column.
    """
    cols = set(grain) | {m.sql for m in kpi.base_measures} | set(kpi.dimensions)
    if model.kind == "sql" and model.output_schema:
        cols.update(model.output_schema)
        return cols
    for dataset in datasets.values():
        cols.update(dataset.columns)
    return cols


@traced
def bind_filters(
    remaining: tuple[IncomingFilter, ...],
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    extract_columns: set[str],
) -> tuple[BoundFilter, ...]:
    """Map leftover filters to source columns. Unmapped filters are a hard error."""
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


@traced
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


@traced
def apply_cut_filters(frame, cut: CutSpec, deferred: tuple[BoundFilter, ...]):
    """Apply deferred IN filters on a Pandas frame, skipping this cut's ignore_filters."""
    work = frame
    for item in deferred:
        if _is_ignored(cut, item):
            continue
        if item.column not in work.columns:
            continue
        if not item.values:
            return work.iloc[0:0].copy()
        work = work[work[item.column].isin(list(item.values))]
    return work


def _is_ignored(cut: CutSpec, item: BoundFilter) -> bool:
    """True if this cut lists the filter (by code or column) in ignore_filters."""
    names = _ignore_names(cut)
    return item.code in names or item.column in names or _norm(item.code) in names


def _ignore_names(cut: CutSpec) -> set[str]:
    """Ignore-filter names in both original and normalized form."""
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
    """Find the source column for a filter via mappings, YAML filter_map, or name match."""
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
    """Build filter_code → column from context mappings and optional YAML filter_map."""
    index: dict[str, str] = {}
    for dataset in datasets.values():
        for mapping in dataset.mappings:
            index[_norm(mapping.filter_code)] = mapping.column_name
    for key, column in kpi.filter_map.items():
        index[_norm(key)] = column
    return index


def _norm(value: str) -> str:
    """Case-insensitive compare key (spaces become underscores)."""
    return value.strip().lower().replace(" ", "_")
