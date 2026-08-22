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
    resolve_requested_graph,
    same_model_id,
)
from kpi_engine.catalog.ops_impl import (
    apply_dimension_maps,
    apply_pandas_facts,
    collapse_pandas_detail,
    pandas_group_keys,
)
from kpi_engine.core.calc_engine import compute_cuts, densify
from kpi_engine.core.cuts import emitted_cuts, finest_grain
from kpi_engine.core.filters import bind_filters, columns_for_source_filters, split_for_duckdb
from kpi_engine.core.model_sql import NON_ADDITIVE, compile_extract, extract
from kpi_engine.core.relations import join_monthly
from kpi_engine.core.time_planner import plan_time
from kpi_engine.dates import add_periods, iso_period
from kpi_engine.exceptions import BindError, KPIEngineError
from kpi_engine.identifiers import norm_name
from kpi_engine.platform import acquire_connection
from kpi_engine.runlog import (
    end_run,
    exception as log_exception,
    log_context,
    log_sql,
    log_step,
    peek_kpi_id,
    peek_request_id,
    start_run,
    traced,
)


def compute(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None = None,
    connection: Any | None = None,
    log_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the full KPI request: bind YAML, extract in DuckDB, calculate in Pandas, return JSON."""
    start_run(
        "compute",
        kpi_id=peek_kpi_id(context),
        request_id=peek_request_id(context),
        log_dir=log_dir,
    )
    try:
        return _compute(
            context, config_dir=config_dir, connection=connection
        )
    except Exception:
        log_exception("compute failed")
        raise
    finally:
        end_run()


def _compute(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None,
    connection: Any | None,
) -> dict[str, Any]:
    """Pipeline body. Logging for this request is already open."""
    log_step("START compute")
    log_context(context)
    root = Path(config_dir) if config_dir else default_config_dir()
    log_step("adapt")
    request = adapt(context)
    log_step("bind")
    kpi = load_kpi(request.kpi_id, root)
    assert_measure_keys(kpi, request.measure_keys)
    requested, needed_bases = resolve_requested_graph(kpi, request.measure_keys)
    scoped = replace(kpi, base_measures=needed_bases)
    models = _models_for_kpi(scoped, root)
    datasets = {}
    for model in models:
        datasets.update(bind_datasets(model, request))
    log_step("plan_time")
    time_plan, remaining_filters = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns: set[str] = set()
    for model in models:
        extract_columns |= columns_for_source_filters(model, scoped, grain, datasets)
    bound = bind_filters(remaining_filters, kpi, datasets, extract_columns)
    source_filters, deferred = split_for_duckdb(bound, emitted)
    log_step("extract")
    con, owned = acquire_connection(connection)
    try:
        monthly, detail, sqls = _extract_all(
            models=models,
            kpi=kpi,
            needed_bases=needed_bases,
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
    log_step("calculate")
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
    log_step("paginate")
    page_rows, pagination = _paginate(rows, request)
    parameters = _parameters(kpi, time_plan)
    result = {
        "kpi_id": kpi.kpi_id,
        "request_id": request.request_id,
        "parameters": parameters,
        "applied_filters": _applied(source_filters, deferred, emitted),
        "ignored_filters": _ignored(deferred, emitted),
        "trend_axes": trend_axes,
        "pagination": pagination,
        "sql": sqls[0] if sqls else "",
        "sqls": sqls,
        "rows": page_rows,
    }
    log_step(
        "END compute",
        kpi_id=kpi.kpi_id,
        row_count=len(page_rows),
        sql_count=len(sqls),
        pagination=pagination,
    )
    return result


def validate(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind and plan without scanning ADLS. Returns the compiled DuckDB SQL."""
    start_run(
        "validate",
        kpi_id=peek_kpi_id(context),
        request_id=peek_request_id(context),
        log_dir=log_dir,
    )
    try:
        return _validate(context, config_dir=config_dir)
    except Exception:
        log_exception("validate failed")
        raise
    finally:
        end_run()


def _validate(
    context: dict[str, Any], *, config_dir: str | Path | None
) -> dict[str, Any]:
    """Validate body. Logging for this request is already open."""
    log_step("START validate")
    log_context(context)
    root = Path(config_dir) if config_dir else default_config_dir()
    request = adapt(context)
    kpi = load_kpi(request.kpi_id, root)
    assert_measure_keys(kpi, request.measure_keys)
    _requested, needed_bases = resolve_requested_graph(kpi, request.measure_keys)
    scoped = replace(kpi, base_measures=needed_bases)
    models = _models_for_kpi(scoped, root)
    datasets = {}
    for model in models:
        datasets.update(bind_datasets(model, request))
    time_plan, remaining = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns: set[str] = set()
    for model in models:
        extract_columns |= columns_for_source_filters(model, scoped, grain, datasets)
    bound = bind_filters(remaining, kpi, datasets, extract_columns)
    source_filters, _deferred = split_for_duckdb(bound, emitted)
    sqls: list[str] = []
    param_count = 0
    for model in models:
        bound_ds = bind_datasets(model, request)
        filter_cols = _model_filter_columns(model, bound_ds, grain, scoped)
        owned = _owned_bases(needed_bases, kpi, model)
        sub = replace(kpi, base_measures=tuple(owned), model_id=model.model_id)
        sql, params = compile_extract(
            model=model,
            kpi=sub,
            datasets=bound_ds,
            source_filters=source_filters,
            plan=time_plan,
            grain=grain,
            filter_columns=filter_cols,
        )
        log_sql(sql, params, model=model.model_id, row_level=True)
        sqls.append(sql)
        param_count += len(params)
    result = {
        "ok": True,
        "kpi_id": kpi.kpi_id,
        **_parameters(kpi, time_plan),
        "sql": sqls[0] if sqls else "",
        "sqls": sqls,
        "param_count": param_count,
    }
    log_step("END validate", kpi_id=kpi.kpi_id, sql_count=len(sqls), param_count=param_count)
    return result


def _models_for_kpi(kpi: KpiSpec, root: Path) -> list[ModelSpec]:
    """Load each distinct model referenced by (needed) base_measures, folded by name."""
    ids: list[str] = []
    seen: set[str] = set()
    for measure in kpi.base_measures:
        mid = measure.model_id or kpi.model_id
        key = norm_name(mid)
        if key in seen:
            continue
        seen.add(key)
        ids.append(mid)
    if not ids and kpi.model_id:
        ids.append(kpi.model_id)
    return [load_model(mid, root) for mid in ids]


def _owned_bases(
    bases: tuple, kpi: KpiSpec, model: ModelSpec
) -> list:
    """Base measures that belong to this model after folding Sotif / sotif."""
    owned = [
        measure
        for measure in bases
        if same_model_id(measure.model_id or kpi.model_id, model.model_id)
    ]
    if bases and not owned:
        declared = [(m.name, m.model_id or kpi.model_id) for m in bases]
        raise BindError(
            f"No base_measures attach to model {model.model_id!r}. "
            f"Declared: {declared}."
        )
    return owned


def _model_filter_columns(
    model: ModelSpec, datasets: dict, grain: tuple[str, ...], kpi: KpiSpec
) -> set[str]:
    """Columns this model's extract can IN-filter (skip other models' fields)."""
    cols = columns_for_source_filters(model, kpi, grain, datasets)
    for dataset in datasets.values():
        cols.update(dataset.columns)
    cols.update(model.output_schema)
    if kpi.time is not None:
        cols.add(kpi.time.column)
    return cols


@traced
def _extract_all(
    *,
    models: list[ModelSpec],
    kpi: KpiSpec,
    needed_bases: tuple,
    datasets: dict,
    source_filters,
    plan,
    grain: tuple[str, ...],
    request: AdaptedRequest,
    connection: Any | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]:
    """Per-model row-level retrieve of physical columns, then Pandas builds the monthly frame."""
    scoped = replace(kpi, base_measures=needed_bases)
    frames: dict[str, pd.DataFrame] = {}
    detail_parts: list[pd.DataFrame] = []
    sqls: list[str] = []
    for model in models:
        bound_ds = bind_datasets(model, request)
        filter_cols = _model_filter_columns(model, bound_ds, grain, scoped)
        owned = _owned_bases(needed_bases, kpi, model)
        sub = replace(kpi, base_measures=tuple(owned), model_id=model.model_id)
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
        sqls.append(extracted.sql)
        raw = extracted.frame
        mapped = apply_dimension_maps(raw, kpi)
        detail_parts.append(apply_pandas_facts(mapped, sub) if not mapped.empty else mapped)
        pandas_monthly = collapse_pandas_detail(raw, sub, grain)
        if not pandas_monthly.empty:
            frames[model.model_id] = pandas_monthly
    detail = pd.concat(detail_parts, ignore_index=True) if detail_parts else None
    if frames:
        monthly = join_monthly(frames, scoped)
    else:
        cols = [kpi.time.column, "_observed"] if kpi.time is not None else ["_observed"]
        monthly = pd.DataFrame(columns=cols)
    monthly = apply_dimension_maps(monthly, kpi)
    monthly = _to_monthly(monthly, scoped, grain, plan)
    return monthly, detail, sqls


def _to_monthly(
    frame: pd.DataFrame, kpi: KpiSpec, grain: tuple[str, ...], plan: TimePlan | None
) -> pd.DataFrame:
    """Place the DuckDB extract on a dense month spine from span_start through the anchor.

    Snapshot KPIs have no period column; the extract is already one row per combo.
    """
    if kpi.time is None or plan is None:
        work = frame.copy()
        if "_observed" not in work.columns:
            work["_observed"] = True
        return work
    time_col = kpi.time.column
    keys = pandas_group_keys(kpi, grain)
    value_cols = _monthly_value_cols(kpi)
    fill_zero = [m.name for m in kpi.base_measures if m.agg in {"sum", "count"}]
    densify_end = plan.anchor
    if plan.lookback_forward:
        densify_end = add_periods(plan.anchor, plan.lookback_forward, kpi.time)
    kwargs = dict(
        keys=keys,
        time_col=time_col,
        start=plan.span_start,
        end=densify_end,
        value_cols=value_cols,
        fill_zero_cols=fill_zero,
        time_spec=kpi.time,
    )
    if frame.empty:
        return densify(pd.DataFrame(columns=[*([time_col] if time_col else []), *keys, *value_cols]), **kwargs)
    return densify(frame, **kwargs)


def _monthly_value_cols(kpi: KpiSpec) -> list[str]:
    """Columns densify must carry (named facts plus avg carry columns)."""
    cols: list[str] = []
    for measure in kpi.base_measures:
        if measure.agg in NON_ADDITIVE:
            continue
        if measure.agg == "avg":
            cols.extend([f"{measure.name}__sum", f"{measure.name}__count"])
            continue
        cols.append(measure.name)
    return cols


def _parameters(kpi: KpiSpec, time_plan: TimePlan | None) -> dict[str, Any]:
    """Anchor / span for the response. Snapshot KPIs have no period clock."""
    if kpi.time is None or time_plan is None:
        return {
            "anchor": None,
            "time_grain": None,
            "span_start": None,
            "lookback_months": 0,
        }
    return {
        "anchor": iso_period(time_plan.anchor, kpi.time),
        "time_grain": kpi.time.grain,
        "span_start": iso_period(time_plan.span_start, kpi.time),
        "lookback_months": time_plan.lookback_months,
    }


def _sort_rows(rows: list[dict[str, Any]], kpi: KpiSpec) -> list[dict[str, Any]]:
    """Stable sort: output_cut, then each dimension in YAML order."""
    dim_order = list(kpi.dimensions)

    def key(row: dict[str, Any]) -> tuple:
        """Sort key for one result row."""
        return (str(row.get("output_cut") or ""), *[str(row.get(d) or "") for d in dim_order])

    return sorted(rows, key=key)


@traced
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
