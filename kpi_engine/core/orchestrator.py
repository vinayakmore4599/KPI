"""Request lifecycle: adapt → bind → plan → extract → calculate → JSON.

What this file provides
    compute(context) — full pipeline, returns the JSON contract (rows, axes,
    applied/ignored filters, compiled sql, pagination).
    validate(context) — same through compile_extract; no file scan.

Where it is used
    kpi_engine.compute / validate re-export these. udfs.sotif.main calls compute.

Capabilities
    One DuckDB session per compute, from the platform helper (or a local
    fallback in tests). Never closes a host connection. Builds extract column
    set, splits filters, densifies the monthly frame, sorts and paginates
    (null page_size = all rows).

When to use
    Change step order only with an architecture decision. Do not add KPI-specific
    if kpi_id == 3004 branches — that belongs in YAML.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

from kpi_engine.contracts import AdaptedRequest, KpiSpec, ModelSpec, TimePlan
from kpi_engine.core.adapter import adapt
from kpi_engine.core.binder import (
    assert_measure_keys,
    bind_datasets,
    default_config_dir,
    load_kpi,
    load_model,
)
from kpi_engine.core.calc_engine import compute_cuts, densify
from kpi_engine.core.cuts import emitted_cuts, finest_grain
from kpi_engine.core.filters import bind_filters, columns_for_source_filters, split_for_duckdb
from kpi_engine.core.model_sql import NON_ADDITIVE, compile_extract, extract
from kpi_engine.core.relations import join_monthly
from kpi_engine.core.time_planner import plan_time
from kpi_engine.dates import iso_period
from kpi_engine.exceptions import KPIEngineError
from kpi_engine.platform import acquire_connection


def compute(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None = None,
    connection: Any | None = None,
) -> dict[str, Any]:
    """Run the full KPI request: bind YAML, extract in DuckDB, calculate in Pandas, return JSON."""
    root = Path(config_dir) if config_dir else default_config_dir()
    request = adapt(context)
    kpi = load_kpi(request.kpi_id, root)
    assert_measure_keys(kpi, request.measure_keys)
    models = _models_for_kpi(kpi, root)
    datasets = {}
    for model in models:
        datasets.update(bind_datasets(model, request))
    time_plan, remaining_filters = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns: set[str] = set()
    for model in models:
        extract_columns |= columns_for_source_filters(model, kpi, grain, datasets)
    bound = bind_filters(remaining_filters, kpi, datasets, extract_columns)
    source_filters, deferred = split_for_duckdb(bound, emitted)
    con, owned = acquire_connection(connection)
    try:
        monthly, detail, sqls = _extract_all(
            models=models,
            kpi=kpi,
            datasets=datasets,
            source_filters=source_filters,
            plan=time_plan,
            grain=grain,
            request=request,
            connection=con,
        )
    finally:
        if owned:
            con.close()
    requested = request.measure_keys or tuple(m.key for m in kpi.measures)
    rows, trend_axes = compute_cuts(
        monthly,
        kpi=kpi,
        emitted=emitted,
        deferred_filters=deferred,
        plan=time_plan,
        requested=requested,
        detail=detail,
    )
    rows = _sort_rows(rows, kpi)
    page_rows, pagination = _paginate(rows, request)
    return {
        "kpi_id": kpi.kpi_id,
        "request_id": request.request_id,
        "parameters": {
            "anchor": iso_period(time_plan.anchor, kpi.time),
            "time_grain": kpi.time.grain,
            "span_start": iso_period(time_plan.span_start, kpi.time),
            "lookback_months": time_plan.lookback_months,
        },
        "applied_filters": _applied(source_filters, deferred, emitted),
        "ignored_filters": _ignored(deferred, emitted),
        "trend_axes": trend_axes,
        "pagination": pagination,
        "sql": sqls[0] if sqls else "",
        "sqls": sqls,
        "rows": page_rows,
    }


def validate(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind and plan without scanning ADLS. Returns the compiled DuckDB SQL."""
    root = Path(config_dir) if config_dir else default_config_dir()
    request = adapt(context)
    kpi = load_kpi(request.kpi_id, root)
    assert_measure_keys(kpi, request.measure_keys)
    models = _models_for_kpi(kpi, root)
    datasets = {}
    for model in models:
        datasets.update(bind_datasets(model, request))
    time_plan, remaining = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns: set[str] = set()
    for model in models:
        extract_columns |= columns_for_source_filters(model, kpi, grain, datasets)
    bound = bind_filters(remaining, kpi, datasets, extract_columns)
    source_filters, _deferred = split_for_duckdb(bound, emitted)
    sqls: list[str] = []
    param_count = 0
    for model in models:
        bound_ds = bind_datasets(model, request)
        filter_cols = _model_filter_columns(model, bound_ds, grain, kpi)
        add_m = [
            m
            for m in kpi.base_measures
            if (m.model_id or kpi.model_id) == model.model_id and m.agg not in NON_ADDITIVE
        ]
        na_m = [
            m
            for m in kpi.base_measures
            if (m.model_id or kpi.model_id) == model.model_id and m.agg in NON_ADDITIVE
        ]
        for row_level, measures in ((False, add_m), (True, na_m)):
            if not measures:
                continue
            sub = replace(kpi, base_measures=tuple(measures), model_id=model.model_id)
            sql, params = compile_extract(
                model=model,
                kpi=sub,
                datasets=bound_ds,
                source_filters=source_filters,
                plan=time_plan,
                grain=grain,
                row_level=row_level,
                filter_columns=filter_cols,
            )
            sqls.append(sql)
            param_count += len(params)
    return {
        "ok": True,
        "kpi_id": kpi.kpi_id,
        "anchor": iso_period(time_plan.anchor, kpi.time),
        "span_start": iso_period(time_plan.span_start, kpi.time),
        "lookback_months": time_plan.lookback_months,
        "sql": sqls[0] if sqls else "",
        "sqls": sqls,
        "param_count": param_count,
    }


def _models_for_kpi(kpi: KpiSpec, root: Path) -> list[ModelSpec]:
    """Load each distinct model referenced by base_measures (default: KPI model)."""
    ids: list[str] = []
    for measure in kpi.base_measures:
        mid = measure.model_id or kpi.model_id
        if mid not in ids:
            ids.append(mid)
    return [load_model(mid, root) for mid in ids]


def _model_filter_columns(
    model: ModelSpec, datasets: dict, grain: tuple[str, ...], kpi: KpiSpec
) -> set[str]:
    """Columns this model's extract can IN-filter (skip other models' fields)."""
    cols = columns_for_source_filters(model, kpi, grain, datasets)
    for dataset in datasets.values():
        cols.update(dataset.columns)
    cols.update(model.output_schema)
    cols.add(kpi.time.column)
    return cols


def _extract_all(
    *,
    models: list[ModelSpec],
    kpi: KpiSpec,
    datasets: dict,
    source_filters,
    plan,
    grain: tuple[str, ...],
    request: AdaptedRequest,
    connection: Any | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]:
    """Per-model additive GROUP BY extracts plus optional row-level detail for non-additive aggs."""
    frames: dict[str, pd.DataFrame] = {}
    detail_parts: list[pd.DataFrame] = []
    sqls: list[str] = []
    for model in models:
        bound_ds = bind_datasets(model, request)
        filter_cols = _model_filter_columns(model, bound_ds, grain, kpi)
        add_m = [
            m
            for m in kpi.base_measures
            if (m.model_id or kpi.model_id) == model.model_id and m.agg not in NON_ADDITIVE
        ]
        na_m = [
            m
            for m in kpi.base_measures
            if (m.model_id or kpi.model_id) == model.model_id and m.agg in NON_ADDITIVE
        ]
        if add_m:
            sub = replace(kpi, base_measures=tuple(add_m), model_id=model.model_id)
            extracted = extract(
                model=model,
                kpi=sub,
                datasets=bound_ds,
                source_filters=source_filters,
                plan=plan,
                grain=grain,
                filter_columns=filter_cols,
                connection=connection,
            )
            frames[model.model_id] = extracted.frame
            sqls.append(extracted.sql)
        if na_m:
            sub = replace(kpi, base_measures=tuple(na_m), model_id=model.model_id)
            extracted = extract(
                model=model,
                kpi=sub,
                datasets=bound_ds,
                source_filters=source_filters,
                plan=plan,
                grain=grain,
                row_level=True,
                filter_columns=filter_cols,
                connection=connection,
            )
            detail_parts.append(extracted.frame)
            sqls.append(extracted.sql)
    if frames:
        monthly = join_monthly(frames, kpi)
        monthly = _to_monthly(monthly, kpi, grain, plan)
    else:
        monthly = pd.DataFrame(columns=[kpi.time.column, "_observed"])
    detail = pd.concat(detail_parts, ignore_index=True) if detail_parts else None
    return monthly, detail, sqls


def _to_monthly(
    frame: pd.DataFrame, kpi: KpiSpec, grain: tuple[str, ...], plan: TimePlan
) -> pd.DataFrame:
    """Place the DuckDB extract on a dense month spine from span_start through the anchor."""
    time_col = kpi.time.column
    keys = [c for c in grain if c != time_col]
    value_cols = [m.name for m in kpi.base_measures]
    for measure in kpi.base_measures:
        if measure.agg == "avg":
            value_cols.extend([f"{measure.name}__sum", f"{measure.name}__count"])
    fill_zero = [m.name for m in kpi.base_measures if m.agg in {"sum", "count"}]
    kwargs = dict(
        keys=keys,
        time_col=time_col,
        start=plan.span_start,
        end=plan.anchor,
        value_cols=value_cols,
        fill_zero_cols=fill_zero,
        time_spec=kpi.time,
    )
    if frame.empty:
        return densify(pd.DataFrame(columns=[*grain, *value_cols]), **kwargs)
    return densify(frame, **kwargs)


def _sort_rows(rows: list[dict[str, Any]], kpi: KpiSpec) -> list[dict[str, Any]]:
    """Stable sort: output_cut, then each dimension in YAML order."""
    dim_order = list(kpi.dimensions)

    def key(row: dict[str, Any]) -> tuple:
        """Sort key for one result row."""
        return (str(row.get("output_cut") or ""), *[str(row.get(d) or "") for d in dim_order])

    return sorted(rows, key=key)


def _paginate(
    rows: list[dict[str, Any]], request: AdaptedRequest
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Slice rows after calculation. Null page_size returns the full list."""
    page_size = request.pagination.page_size or request.pagination.limit
    total = len(rows)
    if page_size is None:
        return rows, {
            "page": request.pagination.page,
            "page_size": None,
            "total_count": total,
            "has_more": False,
        }
    page = request.pagination.page
    if page is None:
        page = 1
    if page < 1:
        raise KPIEngineError("output.page must be >= 1.")
    start = (page - 1) * page_size
    end = start + page_size
    sliced = rows[start:end]
    return sliced, {
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "has_more": end < total,
    }


def _applied(source_filters, deferred, emitted) -> list[dict[str, Any]]:
    """Metadata: filters applied in DuckDB (source) or on a cut in Pandas."""
    rows = [
        {
            "filter_code": f.code,
            "column": f.column,
            "op": "in",
            "values": list(f.values),
            "stage": "source",
        }
        for f in source_filters
    ]
    for item in deferred:
        rows.append(
            {
                "filter_code": item.code,
                "column": item.column,
                "op": "in",
                "values": list(item.values),
                "stage": "cut",
            }
        )
    return rows


def _ignored(deferred, emitted) -> list[dict[str, Any]]:
    """Metadata: filters skipped on a cut because of ignore_filters (e.g. region on G)."""
    rows: list[dict[str, Any]] = []
    for cut in emitted:
        for item in deferred:
            ignore = {x.lower() for x in cut.ignore_filters}
            if item.code.lower() in ignore or item.column.lower() in ignore:
                rows.append(
                    {
                        "filter_code": item.code,
                        "reason": f"cut_{cut.name}_ignore_filters",
                    }
                )
    return rows
