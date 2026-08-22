"""Bind remaining filters to columns, operators, and apply stages.

What this file provides
    bind_filters — map filter_code → column (YAML filters, mappings, filter_map).
    split_filters — extract (DuckDB) vs calc (Pandas) vs result (JSON rows).
    apply_cut_filters / apply_frame_filters — Pandas mask (same ops as SQL).
    apply_result_filters — drop output rows after compute_cuts.

Where it is used
    orchestrator after plan_time. calc_engine per-cut path for G vs R region.

Capabilities
    YAML `filters:` sets op / optional / apply. Undeclared context codes stay
    IN at extract unless a cut lists them in ignore_filters (then calc).
    Empty IN (not optional) matches nothing. Optional omitted/null is skipped.
    extract + ignore_filters is a bind error (declared in binder).

When to use
    Change mapping rules if metadata mappings change. Hierarchy (heir) is
    rejected in the adapter, not here.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from kpi_engine.contracts import (
    BoundFilter,
    CutSpec,
    DatasetBinding,
    FilterApplySpec,
    IncomingFilter,
    KpiSpec,
    ModelSpec,
)
from kpi_engine.core.compose import expand_compose, strip_compose_keys
from kpi_engine.core.filter_ops import assert_filter_arity, pandas_mask
from kpi_engine.exceptions import BindError, FilterError, TimePlanError
from kpi_engine.identifiers import match_name, norm_name
from kpi_engine.runlog import traced


@traced
def columns_for_source_filters(
    model: ModelSpec,
    kpi: KpiSpec,
    grain: tuple[str, ...],
    datasets: dict[str, DatasetBinding],
) -> set[str]:
    """Columns DuckDB may filter: KPI grain/dims, output_schema, and dataset columns.

    SQL models wrap the whole CTE script; filters apply on that final SELECT.
    Physical models expose every context dataset column. No exclusive
    output_schema-only set — KPI-named and mapped columns stay filterable.
    """
    cols = set(grain) | set(kpi.dimensions)
    for measure in kpi.base_measures:
        if measure.sql:
            cols.add(measure.sql)
        cols.update(measure.columns)
        if measure.where is not None:
            cols.add(measure.where.column)
    cols.update(model.output_schema)
    if kpi.time is not None:
        cols.add(kpi.time.column)
    for spec in kpi.filter_specs:
        cols.add(spec.column)
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
    remaining = _apply_filter_composes(remaining, kpi.filter_specs)
    mappings = _mapping_index(datasets, kpi)
    specs = _spec_index(kpi)
    _assert_required_filters(remaining, specs)
    bound: list[BoundFilter] = []
    for item in remaining:
        spec = _spec_for(item, specs)
        if spec is not None and _skip_optional(spec, item):
            continue
        column = _resolve_column(item, mappings, kpi, extract_columns, spec=spec)
        canonical = match_name(column, extract_columns) or column
        if match_name(canonical, extract_columns) is None:
            raise FilterError(
                f"Filter {item.raw_key!r} does not bind to a source column. "
                "Unmapped filters are a hard error."
            )
        op = spec.op if spec is not None else "in"
        values = () if op in {"is_null", "is_not_null"} else item.values
        assert_filter_arity(op, values, code=item.code)
        bound.append(
            BoundFilter(
                code=item.code,
                column=canonical,
                values=values,
                stage=spec.apply if spec is not None else "extract",
                op=op,
                optional=bool(spec.optional) if spec is not None else False,
                input_text=item.input_text,
            )
        )
    return tuple(bound)


@traced
def split_filters(
    bound: tuple[BoundFilter, ...], emitted: tuple[CutSpec, ...]
) -> tuple[tuple[BoundFilter, ...], tuple[BoundFilter, ...], tuple[BoundFilter, ...]]:
    """Split into DuckDB extract, Pandas calc, and post-cut result filters."""
    ignored = {code for cut in emitted for code in _ignore_names(cut)}
    extract: list[BoundFilter] = []
    calc: list[BoundFilter] = []
    result: list[BoundFilter] = []
    for item in bound:
        if item.stage == "result":
            result.append(item)
            continue
        if item.stage == "calc":
            calc.append(item)
            continue
        if _is_listed(item, ignored):
            calc.append(replace(item, stage="calc"))
        else:
            extract.append(replace(item, stage="extract") if item.stage != "extract" else item)
    return tuple(extract), tuple(calc), tuple(result)


@traced
def split_for_duckdb(
    bound: tuple[BoundFilter, ...], emitted: tuple[CutSpec, ...]
) -> tuple[tuple[BoundFilter, ...], tuple[BoundFilter, ...]]:
    """Filters ignored by any emitted cut stay out of DuckDB so G can see all regions."""
    extract, calc, _result = split_filters(bound, emitted)
    return extract, calc


def filters_on_all_cuts(
    calc_filters: tuple[BoundFilter, ...], emitted: tuple[CutSpec, ...]
) -> tuple[BoundFilter, ...]:
    """Calc filters no emitted cut ignores — safe to apply once before densify."""
    return tuple(
        item
        for item in calc_filters
        if not any(_is_ignored(cut, item) for cut in emitted)
    )


@traced
def apply_cut_filters(frame, cut: CutSpec, deferred: tuple[BoundFilter, ...]):
    """Apply calc filters on a Pandas frame, skipping this cut's ignore_filters."""
    work = frame
    if work is None:
        return work
    for item in deferred:
        if _is_ignored(cut, item):
            continue
        work = apply_bound_filter(work, item)
        if work.empty:
            return work
    return work


def apply_frame_filters(frame, items: tuple[BoundFilter, ...]):
    """Apply every filter to a Pandas frame (no per-cut ignore)."""
    work = frame
    if work is None:
        return work
    for item in items:
        work = apply_bound_filter(work, item)
        if getattr(work, "empty", False):
            return work
    return work


def apply_bound_filter(frame, item: BoundFilter):
    """Mask `frame` with one bound filter. Missing columns are a no-op."""
    if frame is None:
        return frame
    col = match_name(item.column, frame.columns)
    if col is None:
        return frame
    mask = pandas_mask(frame[col], item.op, item.values)
    return frame[mask]


def apply_result_filters(rows: list[dict], items: tuple[BoundFilter, ...]) -> list[dict]:
    """Drop JSON rows after measures. Missing dimension keys are a no-op."""
    if not items or not rows:
        return rows
    frame = pd.DataFrame(rows)
    keep = pd.Series(True, index=frame.index)
    for item in items:
        col = match_name(item.column, frame.columns)
        if col is None:
            continue
        keep = keep & pandas_mask(frame[col], item.op, item.values)
    return [row for row, ok in zip(rows, keep.tolist()) if bool(ok)]


def _skip_optional(spec: FilterApplySpec, item: IncomingFilter) -> bool:
    """optional: true and omitted/null comparison values → do not apply."""
    if not spec.optional:
        return False
    if spec.op in {"is_null", "is_not_null"}:
        return False
    return _is_blank(item.values)


def _is_blank(values: tuple) -> bool:
    """True when the context sent no comparison value (or only nulls)."""
    if not values:
        return True
    return all(value is None for value in values)


def _apply_filter_composes(
    remaining: tuple[IncomingFilter, ...], specs: tuple
) -> tuple[IncomingFilter, ...]:
    """Build synthetic IncomingFilters from filters:.compose and drop part keys."""
    work = remaining
    extras: list[IncomingFilter] = []
    for spec in specs:
        template = spec.compose_template
        if not template:
            continue
        existing = next(
            (
                item
                for item in work
                if norm_name(item.code) == norm_name(spec.code)
                or norm_name(item.raw_key) == norm_name(spec.code)
            ),
            None,
        )
        if existing is not None and len(existing.values) == 1 and existing.values[0] is not None:
            work = strip_compose_keys(work, template)
            continue
        try:
            value, _consumed = expand_compose(
                template, work, what=f"filters.{spec.code}.compose.template"
            )
        except TimePlanError as exc:
            if spec.optional:
                work = strip_compose_keys(work, template)
                continue
            raise BindError(str(exc)) from exc
        extras.append(
            IncomingFilter(
                raw_key=spec.code,
                code=spec.code,
                values=(value,),
                input_text=None,
            )
        )
        work = strip_compose_keys(work, template)
        if existing is not None:
            work = tuple(
                item
                for item in work
                if norm_name(item.code) != norm_name(spec.code)
                and norm_name(item.raw_key) != norm_name(spec.code)
            )
    return tuple(extras) + work


def _assert_required_filters(
    remaining: tuple[IncomingFilter, ...], specs: dict[str, FilterApplySpec]
) -> None:
    """optional: false specs must appear on the context."""
    present = {norm_name(item.code) for item in remaining} | {
        norm_name(item.raw_key) for item in remaining
    }
    for spec in specs.values():
        if spec.optional:
            continue
        if spec.compose_template:
            continue
        if norm_name(spec.code) not in present:
            raise BindError(f"Required filter {spec.code!r} is missing from context.")


def _spec_index(kpi: KpiSpec) -> dict[str, FilterApplySpec]:
    """Folded filter code → YAML spec."""
    return {norm_name(spec.code): spec for spec in kpi.filter_specs}


def _spec_for(item: IncomingFilter, specs: dict[str, FilterApplySpec]) -> FilterApplySpec | None:
    """YAML spec for this context filter, if declared."""
    return specs.get(norm_name(item.code)) or specs.get(norm_name(item.raw_key))


def _is_ignored(cut: CutSpec, item: BoundFilter) -> bool:
    """True if this cut lists the filter (by code or column) in ignore_filters."""
    return _is_listed(item, _ignore_names(cut))


def _is_listed(item: BoundFilter, names: set[str]) -> bool:
    """True if the filter code or column is in a folded ignore set."""
    return (
        item.code in names
        or item.column in names
        or norm_name(item.code) in names
        or norm_name(item.column) in names
    )


def _ignore_names(cut: CutSpec) -> set[str]:
    """Ignore-filter names in both original and normalized form."""
    names: set[str] = set()
    for raw in cut.ignore_filters:
        names.add(raw)
        names.add(norm_name(raw))
    return names


def _resolve_column(
    item: IncomingFilter,
    mappings: dict[str, str],
    kpi: KpiSpec,
    extract_columns: set[str],
    spec: FilterApplySpec | None = None,
) -> str:
    """Find the source column via YAML filters, mappings, filter_map, or name match."""
    if spec is not None:
        return match_name(spec.column, extract_columns) or spec.column
    mapped = mappings.get(norm_name(item.code)) or mappings.get(norm_name(item.raw_key))
    if mapped:
        return match_name(mapped, extract_columns) or mapped
    yaml_map = {norm_name(k): v for k, v in kpi.filter_map.items()}
    if norm_name(item.code) in yaml_map:
        mapped = yaml_map[norm_name(item.code)]
        return match_name(mapped, extract_columns) or mapped
    hit = match_name(item.raw_key, extract_columns) or match_name(item.code, extract_columns)
    if hit:
        return hit
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
            index[norm_name(mapping.filter_code)] = mapping.column_name
    for key, column in kpi.filter_map.items():
        index[norm_name(key)] = column
    return index
