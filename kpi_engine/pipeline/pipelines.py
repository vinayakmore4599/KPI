"""Partition a request into per-extract pipelines.

What this file provides
    partition_request — one pipeline per extract model, or one joined pipeline
    when a requested measure graph spans models.
    pad_result_rows — stamp model and null-fill measures from other extracts
    (dimension keys stay as calc stamped them).

Where it is used
    orchestrator compute/validate.

When to use
    Models only retrieve. Cuts and dimensions stay on the KPI YAML.
"""

from __future__ import annotations

from dataclasses import dataclass

from kpi_engine.contracts import (
    BaseMeasure,
    CutSpec,
    DatasetBinding,
    KpiSpec,
    ModelSpec,
    OutputSpec,
)
from kpi_engine.pipeline.binder import measure_dependencies, resolve_requested_graph
from kpi_engine.pipeline.cuts import cut_group_dims, plan_emitted_cuts
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import match_name, norm_name


@dataclass(frozen=True)
class Pipeline:
    """One extract/calc unit: a single model, or several models joined after extract."""

    model_ids: tuple[str, ...]
    measure_keys: tuple[str, ...]
    bases: tuple[BaseMeasure, ...]
    joined: bool


def extract_model_id(base: BaseMeasure, kpi: KpiSpec) -> str:
    """Which model YAML this base reads."""
    return base.model_id or kpi.model_id


def partition_request(kpi: KpiSpec, requested: tuple[str, ...]) -> list[Pipeline]:
    """Group requested measures by extract model; join only when a graph spans models."""
    emit, _bases = resolve_requested_graph(kpi, requested)
    by_key = {m.key: m for m in kpi.measures}
    isolated: dict[str, list[str]] = {}
    join_keys: list[str] = []
    join_models: set[str] = set()

    for key in emit:
        models = _models_for_key(kpi, key, by_key)
        if len(models) > 1:
            if not kpi.model_relations:
                raise BindError(
                    f"measures.{key} spans models {sorted(models)}; "
                    "declare model_relations to join them, or request one extract at a time."
                )
            join_keys.append(key)
            join_models |= models
            continue
        mid = next(iter(models)) if models else norm_name(kpi.model_id)
        isolated.setdefault(mid, []).append(key)

    pipelines: list[Pipeline] = []
    if join_keys:
        extra = [
            measure_key
            for _mid, keys in list(isolated.items())
            for measure_key in keys
            if _models_for_key(kpi, measure_key, by_key) & join_models
        ]
        joined_keys = list(dict.fromkeys([*join_keys, *extra]))
        for mid in list(isolated):
            isolated[mid] = [k for k in isolated[mid] if k not in joined_keys]
            if not isolated[mid]:
                isolated.pop(mid)
        actual_ids = _actual_model_ids(kpi, join_models)
        pipelines.append(
            Pipeline(
                model_ids=actual_ids,
                measure_keys=tuple(joined_keys),
                bases=_bases_for_keys(kpi, joined_keys),
                joined=True,
            )
        )

    for mid, keys in isolated.items():
        actual = _actual_model_ids(kpi, {mid})
        pipelines.append(
            Pipeline(
                model_ids=actual,
                measure_keys=tuple(keys),
                bases=_bases_for_keys(kpi, keys),
                joined=False,
            )
        )
    return pipelines


def cuts_for_keys(kpi: KpiSpec, keys: tuple[str, ...]) -> tuple[CutSpec, ...]:
    """Cuts for these measures after only_cut / emit_cuts / locked_cut / also_emit."""
    return plan_emitted_cuts(kpi, keys)


def available_extract_columns(
    model: ModelSpec,
    datasets: dict[str, DatasetBinding],
    kpi: KpiSpec,
) -> set[str] | None:
    """Columns this extract is known to have, or None if the schema is unknown."""
    cols: set[str] = set(model.output_schema)
    for dataset in datasets.values():
        cols.update(dataset.columns)
    if kpi.time is not None:
        cols.add(kpi.time.column)
    for spec in kpi.dimension_specs:
        source = spec.source or spec.name
        if match_name(source, cols) is not None:
            cols.add(spec.name)
    known = {c for c in cols if kpi.time is None or norm_name(c) != norm_name(kpi.time.column)}
    if not known:
        return None
    return cols


def compatible_cuts(
    cuts: tuple[CutSpec, ...],
    columns: set[str] | None,
    time_column: str | None,
    kpi: KpiSpec | None = None,
) -> tuple[CutSpec, ...]:
    """Cuts whose group_by columns are all on the extract. Unknown schema keeps all."""
    if columns is None:
        return cuts
    rename = {}
    if kpi is not None:
        rename = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}
    kept: list[CutSpec] = []
    for cut in cuts:
        dims = cut_group_dims(cut, time_column or "", kpi)
        ok = True
        for dim in dims:
            physical = rename.get(dim, dim)
            if match_name(dim, columns) is None and match_name(physical, columns) is None:
                ok = False
                break
        if ok:
            kept.append(cut)
    return tuple(kept)


def assert_named_cuts_compatible(
    kpi: KpiSpec, keys: tuple[str, ...], compatible: tuple[CutSpec, ...]
) -> None:
    """Fail if a measure named a cut that this extract cannot group by."""
    if kpi.locked_cut is not None or kpi.only_cut is not None:
        return
    allowed = {c.name for c in compatible}
    by_key = {m.key: m for m in kpi.measures}
    for key in keys:
        spec = by_key.get(key)
        if spec is None:
            continue
        named = spec.cuts if spec.cuts is not None else (kpi.default_cut,)
        for name in named:
            if name not in allowed:
                raise BindError(
                    f"measures.{key} cuts {name!r} is not on this extract "
                    f"(have {sorted(allowed)})."
                )


def pad_result_rows(
    rows: list[dict],
    kpi: KpiSpec,
    requested: tuple[str, ...],
    model_id: str,
) -> list[dict]:
    """Stamp model and null-fill measures from other extracts. Dims stay as stamped."""
    _ = kpi
    for row in rows:
        row["model"] = model_id
        row.setdefault("grouped_dimensions", [])
        for key in requested:
            if key not in row:
                row[key] = None
    return rows


def join_keys_for(
    kpi: KpiSpec,
    model_ids: tuple[str, ...],
    grouping: tuple[str, ...] = (),
    both_columns: set[str] | None = None,
) -> tuple[str, ...]:
    """Join keys for this request: on ∩ (time ∪ grouping) plus request_grain on both extracts."""
    folded = {norm_name(mid) for mid in model_ids}
    names: list[str] = []
    seen: set[str] = set()
    by_base = {b.name: extract_model_id(b, kpi) for b in kpi.base_measures}
    time_col = kpi.time.column if kpi.time is not None else None
    grouping_fold = {norm_name(name) for name in grouping}
    if time_col:
        grouping_fold.add(norm_name(time_col))

    def add(name: str) -> None:
        if name not in seen:
            names.append(name)
            seen.add(name)

    for rel in kpi.model_relations:
        left = by_base.get(rel.left)
        right = by_base.get(rel.right)
        if left is None or right is None:
            continue
        if norm_name(left) not in folded or norm_name(right) not in folded:
            continue
        for name in rel.on:
            if norm_name(name) in grouping_fold:
                add(name)
        for name in kpi.request_grain:
            if both_columns is None:
                add(name)
                continue
            if (
                match_name(name, both_columns) is not None
                or any(
                    match_name(spec.source, both_columns) is not None
                    for spec in kpi.dimension_specs
                    if norm_name(spec.name) == norm_name(name)
                )
            ):
                add(name)
    return tuple(names)


def _models_for_key(kpi: KpiSpec, key: str, by_key: dict[str, OutputSpec]) -> set[str]:
    """Extract models this measure (and its deps) read."""
    models: set[str] = set()
    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for base in kpi.base_measures:
            if base.name == name:
                models.add(norm_name(extract_model_id(base, kpi)))
                return
        spec = by_key.get(name)
        if spec is None:
            return
        if spec.of:
            walk(spec.of)
        for dep in measure_dependencies(spec):
            walk(dep)

    walk(key)
    return models


def _bases_for_keys(kpi: KpiSpec, keys: tuple[str, ...] | list[str]) -> tuple[BaseMeasure, ...]:
    """Base measures those keys need, in KPI order."""
    _, bases = resolve_requested_graph(kpi, tuple(keys))
    return bases


def _actual_model_ids(kpi: KpiSpec, folded: set[str]) -> tuple[str, ...]:
    """Preserve declared spelling for model ids that fold to `folded`."""
    out: list[str] = []
    seen: set[str] = set()
    for base in kpi.base_measures:
        mid = extract_model_id(base, kpi)
        key = norm_name(mid)
        if key in folded and key not in seen:
            out.append(mid)
            seen.add(key)
    if not out and kpi.model_id and norm_name(kpi.model_id) in folded:
        out.append(kpi.model_id)
    return tuple(out)
