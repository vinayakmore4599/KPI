"""Request lifecycle: adapt → bind → plan → extract → calculate → JSON."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from kpi_engine.contracts import AdaptedRequest, KpiSpec
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
from kpi_engine.core.filters import bind_filters, split_for_duckdb
from kpi_engine.core.model_sql import compile_extract, extract
from kpi_engine.core.time_planner import plan_time
from kpi_engine.dates import iso_month
from kpi_engine.exceptions import KPIEngineError


def compute(
    context: dict[str, Any],
    *,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(config_dir) if config_dir else default_config_dir()
    request = adapt(context)
    kpi = load_kpi(request.kpi_id, root)
    assert_measure_keys(kpi, request.measure_keys)
    model = load_model(kpi.model_id, root)
    datasets = bind_datasets(model, request)
    time_plan, remaining_filters = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns = set(grain) | {m.sql for m in kpi.base_measures} | set(kpi.dimensions)
    for dataset in datasets.values():
        extract_columns.update(dataset.columns)
    bound = bind_filters(remaining_filters, kpi, datasets, extract_columns)
    source_filters, deferred = split_for_duckdb(bound, emitted)
    extracted = extract(
        model=model,
        kpi=kpi,
        datasets=datasets,
        source_filters=source_filters,
        plan=time_plan,
        grain=grain,
    )
    monthly = _to_monthly(extracted.frame, kpi, grain, time_plan)
    requested = request.measure_keys or tuple(o.key for o in kpi.outputs)
    rows, trend_axes = compute_cuts(
        monthly,
        kpi=kpi,
        emitted=emitted,
        deferred_filters=deferred,
        plan=time_plan,
        requested=requested,
    )
    rows = _sort_rows(rows, kpi)
    page_rows, pagination = _paginate(rows, request)
    return {
        "kpi_id": kpi.kpi_id,
        "request_id": request.request_id,
        "parameters": {
            "anchor": iso_month(time_plan.anchor),
            "time_grain": kpi.time.grain,
            "span_start": iso_month(time_plan.span_start),
            "lookback_months": time_plan.lookback_months,
        },
        "applied_filters": _applied(source_filters, deferred, emitted),
        "ignored_filters": _ignored(deferred, emitted),
        "trend_axes": trend_axes,
        "pagination": pagination,
        "sql": extracted.sql,
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
    model = load_model(kpi.model_id, root)
    datasets = bind_datasets(model, request)
    time_plan, remaining = plan_time(request, kpi)
    emitted = emitted_cuts(kpi)
    grain = finest_grain(kpi, emitted)
    extract_columns = set(grain) | {m.sql for m in kpi.base_measures}
    for dataset in datasets.values():
        extract_columns.update(dataset.columns)
    bound = bind_filters(remaining, kpi, datasets, extract_columns)
    source_filters, _deferred = split_for_duckdb(bound, emitted)
    sql, params = compile_extract(
        model=model,
        kpi=kpi,
        datasets=datasets,
        source_filters=source_filters,
        plan=time_plan,
        grain=grain,
    )
    return {
        "ok": True,
        "kpi_id": kpi.kpi_id,
        "anchor": iso_month(time_plan.anchor),
        "span_start": iso_month(time_plan.span_start),
        "lookback_months": time_plan.lookback_months,
        "sql": sql,
        "param_count": len(params),
    }


def _to_monthly(
    frame: pd.DataFrame, kpi: KpiSpec, grain: tuple[str, ...], plan: TimePlan
) -> pd.DataFrame:
    time_col = kpi.time.column
    keys = [c for c in grain if c != time_col]
    value_cols = [m.name for m in kpi.base_measures]
    for measure in kpi.base_measures:
        if measure.agg == "avg":
            value_cols.extend([f"{measure.name}__sum", f"{measure.name}__count"])
    fill_zero = [m.name for m in kpi.base_measures if m.agg in {"sum", "count"}]
    if frame.empty:
        return densify(
            pd.DataFrame(columns=[*grain, *value_cols]),
            keys=keys,
            time_col=time_col,
            start=plan.span_start,
            end=plan.anchor,
            value_cols=value_cols,
            fill_zero_cols=fill_zero,
        )
    return densify(
        frame,
        keys=keys,
        time_col=time_col,
        start=plan.span_start,
        end=plan.anchor,
        value_cols=value_cols,
        fill_zero_cols=fill_zero,
    )


def _sort_rows(rows: list[dict[str, Any]], kpi: KpiSpec) -> list[dict[str, Any]]:
    dim_order = list(kpi.dimensions)

    def key(row: dict[str, Any]) -> tuple:
        return (str(row.get("output_cut") or ""), *[str(row.get(d) or "") for d in dim_order])

    return sorted(rows, key=key)


def _paginate(
    rows: list[dict[str, Any]], request: AdaptedRequest
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = request.pagination.page_size or request.pagination.limit
    total = len(rows)
    if page_size is None:
        return rows, {
            "page": request.pagination.page,
            "page_size": None,
            "total_count": total,
            "has_more": False,
        }
    page = request.pagination.page or 1
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
