"""Request lifecycle: adapt → bind → plan → extract → calculate → JSON.

What this file provides
    compute(context) — full pipeline, returns the JSON contract (rows, axes,
    applied/ignored filters, compiled sql, pagination).
    validate(context) — same through compile_extract; no file scan.

Where it is used
    kpi_engine.compute / validate re-export these. kpi_engine.main calls compute.

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
    fold_measure_keys,
    load_kpi,
    load_model,
    resolve_requested_graph,
    same_model_id,
)
from kpi_engine.core.fn_apply import (
    apply_dimension_maps,
    apply_pandas_facts,
    collapse_pandas_detail,
    fold_extract_columns,
    pandas_group_keys,
)
from kpi_engine.core.calc_engine import compute_cuts, densify
from kpi_engine.core.cuts import extract_grain, effective_group_by
from kpi_engine.core.filters import (
    apply_frame_filters,
    apply_result_filters,
    bind_filters,
    columns_for_source_filters,
    filters_on_all_cuts,
    split_filters,
)
from kpi_engine.core.model_sql import NON_ADDITIVE, compile_extract, extract
from kpi_engine.core.pipelines import (
    Pipeline,
    assert_named_cuts_compatible,
    available_extract_columns,
    compatible_cuts,
    cuts_for_keys,
    join_keys_for,
    pad_result_rows,
    partition_request,
)
from kpi_engine.core.relations import join_monthly
from kpi_engine.core.parameters import declared_time_grain
from kpi_engine.core.time_planner import apply_request_time, plan_time, span_for_keys
from kpi_engine.dates import add_periods, iso_period, parse_date, period_label
from kpi_engine.exceptions import BindError, KPIEngineError
from kpi_engine.identifiers import match_name, norm_name
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
    kpi = load_kpi(
        request.kpi_id,
        root,
        parameters=request.parameters,
        selected_dimensions=request.selected_dimensions,
    )
    request, kpi = _apply_host_defaults(request, kpi)
    requested = request.measure_keys
    pipelines = partition_request(kpi, requested)
    models_by_id, datasets = _bind_context_models(kpi, request, root, pipelines)
    _assert_listed_name_clash(kpi, models_by_id, datasets)
    log_step("plan_time")
    time_plan, remaining_filters = plan_time(request, kpi)
    extract_columns = _union_extract_columns(models_by_id, datasets, kpi)
    bound, skipped_filters = bind_filters(remaining_filters, kpi, datasets, extract_columns)
    skipped_filters = list(skipped_filters)
    dropped_cuts: list[dict[str, str]] = []
    applied_emitted: list = []
    seen_applied: set[str] = set()
    log_step("extract")
    con, owned = acquire_connection(connection)
    rows: list[dict[str, Any]] = []
    sqls: list[str] = []
    applied_src: list = []
    applied_def: list = []
    applied_res: list = []
    ignored: list[dict[str, Any]] = []
    trend_axes: dict[str, list[str]] = {}
    dropped_groups: list[dict[str, Any]] = []
    try:
        for pipe in pipelines:
            prepared = _prepare_pipeline(
                pipe, kpi, request, models_by_id, bound, time_plan
            )
            monthly, detail, pipe_sqls = _extract_all(
                models=prepared["models"],
                kpi=kpi,
                needed_bases=pipe.bases,
                datasets=prepared["datasets"],
                source_filters=prepared["source_filters"],
                calc_filters=prepared["global_calc"],
                plan=prepared["plan"],
                grain=prepared["grain"],
                request=request,
                connection=con,
                joined=pipe.joined,
                measure_keys=pipe.measure_keys,
            )
            pipe_rows, axes, pipe_dropped = compute_cuts(
                monthly,
                kpi=replace(kpi, base_measures=pipe.bases),
                emitted=prepared["cuts"],
                deferred_filters=prepared["deferred"],
                plan=prepared["plan"],
                requested=pipe.measure_keys,
                detail=detail,
            )
            pipe_rows, result_skipped = apply_result_filters(
                pipe_rows, prepared["result_filters"], kpi, prepared["cuts"]
            )
            skipped_filters.extend(result_skipped)
            dropped_cuts.extend(prepared["dropped_cuts"])
            for cut in prepared["cuts"]:
                if cut.name not in seen_applied:
                    applied_emitted.append(cut)
                    seen_applied.add(cut.name)
            stamp = "+".join(pipe.model_ids) if pipe.joined else pipe.model_ids[0]
            other = tuple(k for k in requested if k not in pipe.measure_keys)
            pipe_rows = pad_result_rows(pipe_rows, kpi, other, stamp)
            rows.extend(pipe_rows)
            sqls.extend(pipe_sqls)
            trend_axes.update(axes)
            dropped_groups.extend(pipe_dropped)
            applied_src.extend(prepared["source_filters"])
            applied_def.extend(prepared["deferred"])
            applied_res.extend(prepared["result_filters"])
            ignored.extend(_ignored(prepared["deferred"], prepared["cuts"]))
    finally:
        if owned:
            con.close()
    log_step("calculate")
    rows = _stamp_green(_sort_rows(rows, kpi), kpi)
    log_step("paginate")
    page_rows, pagination = _paginate(rows, request)
    parameters = _parameters(kpi, time_plan)
    result = {
        "kpi_id": kpi.kpi_id,
        "request_id": request.request_id,
        "parameters": parameters,
        "request_parameters": dict(kpi.bound_parameters),
        "applied_filters": _dedupe_applied(_applied(applied_src, applied_def, applied_res)),
        "ignored_filters": _dedupe_ignored(ignored),
        "skipped_filters": list(skipped_filters),
        "selected_dimensions": list(kpi.request_grain),
        "applied_cuts": _applied_cuts(kpi, tuple(applied_emitted)),
        "dropped_cuts": _dedupe_dropped(dropped_cuts),
        "dropped_groups": dropped_groups,
        "grain_warnings": _grain_warnings(kpi),
        "trend_axes": trend_axes,
        "trend_labels": _trend_labels(trend_axes, kpi),
        "meta": _response_meta(kpi),
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
    kpi = load_kpi(
        request.kpi_id,
        root,
        parameters=request.parameters,
        selected_dimensions=request.selected_dimensions,
    )
    request, kpi = _apply_host_defaults(request, kpi)
    pipelines = partition_request(kpi, request.measure_keys)
    models_by_id, datasets = _bind_context_models(kpi, request, root, pipelines)
    _assert_listed_name_clash(kpi, models_by_id, datasets)
    time_plan, remaining = plan_time(request, kpi)
    extract_columns = _union_extract_columns(models_by_id, datasets, kpi)
    bound, skipped_filters = bind_filters(remaining, kpi, datasets, extract_columns)
    skipped_filters = list(skipped_filters)
    dropped_cuts: list[dict[str, str]] = []
    applied_emitted: list = []
    seen_applied: set[str] = set()
    sqls: list[str] = []
    param_count = 0
    for pipe in pipelines:
        prepared = _prepare_pipeline(pipe, kpi, request, models_by_id, bound, time_plan)
        dropped_cuts.extend(prepared["dropped_cuts"])
        for cut in prepared["cuts"]:
            if cut.name not in seen_applied:
                applied_emitted.append(cut)
                seen_applied.add(cut.name)
        scoped = replace(kpi, base_measures=pipe.bases)
        for model in prepared["models"]:
            bound_ds = bind_datasets(model, request)
            filter_cols = _model_filter_columns(model, bound_ds, prepared["grain"], scoped)
            owned = _owned_bases(pipe.bases, kpi, model)
            sub = replace(kpi, base_measures=tuple(owned), model_id=model.model_id)
            sql, params = compile_extract(
                model=model,
                kpi=sub,
                datasets=bound_ds,
                source_filters=prepared["source_filters"],
                plan=prepared["plan"],
                grain=prepared["grain"],
                filter_columns=filter_cols,
            )
            log_sql(sql, params, model=model.model_id, row_level=True)
            sqls.append(sql)
            param_count += len(params)
    result = {
        "ok": True,
        "kpi_id": kpi.kpi_id,
        **_parameters(kpi, time_plan),
        "request_parameters": dict(kpi.bound_parameters),
        "sql": sqls[0] if sqls else "",
        "sqls": sqls,
        "param_count": param_count,
        "skipped_filters": list(skipped_filters),
        "selected_dimensions": list(kpi.request_grain),
        "applied_cuts": _applied_cuts(kpi, tuple(applied_emitted)),
        "dropped_cuts": _dedupe_dropped(dropped_cuts),
        "grain_warnings": _grain_warnings(kpi),
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


def _bind_context_models(
    kpi: KpiSpec, request, root: Path, pipelines: list[Pipeline]
) -> tuple[dict[str, ModelSpec], dict]:
    """Bind every KPI model the context can satisfy; required extracts must bind."""
    required = {norm_name(mid) for pipe in pipelines for mid in pipe.model_ids}
    loaded: dict[str, ModelSpec] = {}
    datasets: dict = {}
    for model in _models_for_kpi(kpi, root):
        key = norm_name(model.model_id)
        try:
            bound = bind_datasets(model, request)
        except BindError:
            if key in required:
                if kpi.model_templated:
                    raise BindError(
                        f"Model {model.model_id!r} requires alias "
                        f"{list(model.required_aliases)}; chosen model (templated "
                        f"KPI model {kpi.model_id!r}) needs those aliases on "
                        f"context.datasets."
                    ) from None
                raise
            continue
        loaded[key] = model
        datasets.update(bound)
    for mid in required:
        if mid not in loaded:
            loaded[mid] = load_model(mid, root)
    return loaded, datasets


def _assert_listed_name_clash(
    kpi: KpiSpec, models_by_id: dict[str, ModelSpec], datasets: dict
) -> None:
    """BindError when a writing helper shares a listed extract / output_schema name."""
    listed: set[str] = set()
    for dataset in datasets.values():
        listed.update(norm_name(col) for col in dataset.columns)
    for model in models_by_id.values():
        listed.update(norm_name(col) for col in model.output_schema)
    if not listed:
        return
    for measure in kpi.base_measures:
        writes = bool(measure.expr or measure.lookup or measure.over)
        if writes and not measure.replace and norm_name(measure.name) in listed:
            raise BindError(
                f"base_measures.{measure.name} would overwrite extract column "
                f"{measure.name!r}. Set replace: true if that is intended."
            )


def _union_extract_columns(
    models_by_id: dict[str, ModelSpec], datasets: dict, kpi: KpiSpec
) -> set[str]:
    """Columns any requested extract can bind a filter to."""
    cols: set[str] = set()
    for model in models_by_id.values():
        known = available_extract_columns(model, datasets, kpi)
        if known is None:
            cols |= columns_for_source_filters(model, kpi, (), datasets)
            continue
        cols |= known
    if kpi.time is not None:
        cols.add(kpi.time.column)
    cols.update(kpi.dimensions)
    return cols


def _prepare_pipeline(
    pipe: Pipeline,
    kpi: KpiSpec,
    request,
    models_by_id: dict[str, ModelSpec],
    bound,
    time_plan,
) -> dict[str, Any]:
    """Cuts, grain, filters, and time span for one extract (or join) pipeline."""
    models = [models_by_id[norm_name(mid)] for mid in pipe.model_ids]
    pipe_ds = {}
    available: set[str] | None = set()
    unknown = False
    per_model: list[set[str] | None] = []
    for model in models:
        bound_ds = bind_datasets(model, request)
        pipe_ds.update(bound_ds)
        known = available_extract_columns(model, bound_ds, kpi)
        per_model.append(known)
        if known is None:
            unknown = True
        else:
            available |= known
    if unknown:
        available = None
    candidates = cuts_for_keys(kpi, pipe.measure_keys)
    time_col = kpi.time.column if kpi.time is not None else None
    _assert_request_grain_on_models(kpi, models, available, joined=pipe.joined, per_model=per_model)
    emitted = compatible_cuts(candidates, available, time_col, kpi)
    dropped = [
        {"name": cut.name, "reason": "incompatible_extract"}
        for cut in candidates
        if cut.name not in {c.name for c in emitted}
    ]
    assert_named_cuts_compatible(kpi, pipe.measure_keys, emitted)
    _assert_emitted_includes_default(kpi, emitted, pipe.measure_keys)
    grouping = tuple(
        dict.fromkeys(
            name
            for cut in emitted
            for name in effective_group_by(cut, kpi)
        )
    )
    extra = (
        join_keys_for(kpi, pipe.model_ids, grouping=grouping, both_columns=available)
        if pipe.joined
        else ()
    )
    pipe_bound = tuple(
        item
        for item in bound
        if available is None
        or match_name(item.column, available) is not None
        or (time_col is not None and norm_name(item.column) == norm_name(time_col))
    )
    source_filters, deferred, result_filters = split_filters(pipe_bound, emitted)
    extra = tuple(dict.fromkeys([*extra, *(item.column for item in deferred)]))
    grain = extract_grain(kpi, emitted, pipe.bases, extra=extra)
    from kpi_engine.capabilities.ops.support import assert_helper_of_allowed

    assert_helper_of_allowed(kpi, emitted, pipe.measure_keys)
    return {
        "models": models,
        "datasets": pipe_ds,
        "cuts": emitted,
        "grain": grain,
        "plan": span_for_keys(kpi, pipe.measure_keys, time_plan),
        "source_filters": source_filters,
        "deferred": deferred,
        "result_filters": result_filters,
        "global_calc": filters_on_all_cuts(deferred, emitted),
        "dropped_cuts": dropped,
    }


def _dedupe_applied(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first metadata row per filter code / column / stage."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("filter_code"), row.get("column"), row.get("stage"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_ignored(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first ignore-reason row per filter / reason."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("filter_code"), row.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


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
    calc_filters=(),
    plan,
    grain: tuple[str, ...],
    request: AdaptedRequest,
    connection: Any | None = None,
    joined: bool = False,
    measure_keys: tuple[str, ...] = (),
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
        log_step(
            "extract_frame",
            model=model.model_id,
            row_count=0 if raw is None else len(raw),
            columns=[] if raw is None else list(raw.columns),
        )
        folded = fold_extract_columns(raw, kpi, grain)
        mapped = apply_dimension_maps(folded, kpi)
        mapped = apply_frame_filters(mapped, calc_filters)
        if mapped.empty:
            detail_parts.append(mapped)
            continue
        from kpi_engine.core.row_pipeline import stabilize_detail

        mapped = stabilize_detail(mapped, kpi)
        facted = apply_pandas_facts(mapped, sub)
        detail_parts.append(facted)
        pandas_monthly = collapse_pandas_detail(
            facted, sub, grain, facts_applied=True, measure_keys=measure_keys
        )
        if not pandas_monthly.empty:
            frames[model.model_id] = pandas_monthly
    detail = pd.concat(detail_parts, ignore_index=True) if detail_parts else None
    if frames and joined:
        monthly = join_monthly(frames, scoped)
    elif frames:
        monthly = next(iter(frames.values()))
    else:
        cols = [kpi.time.column, "_observed"] if kpi.time is not None else ["_observed"]
        monthly = pd.DataFrame(columns=cols)
    monthly = apply_dimension_maps(monthly, kpi)
    monthly = apply_frame_filters(monthly, calc_filters)
    monthly = _to_monthly(monthly, scoped, grain, plan, measure_keys=measure_keys)
    return monthly, detail, sqls


def _to_monthly(
    frame: pd.DataFrame,
    kpi: KpiSpec,
    grain: tuple[str, ...],
    plan: TimePlan | None,
    measure_keys: tuple[str, ...] = (),
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
    value_cols = _monthly_value_cols(kpi, measure_keys)
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
        kpi=kpi,
        grain=grain,
    )
    if frame.empty:
        return densify(pd.DataFrame(columns=[*([time_col] if time_col else []), *keys, *value_cols]), **kwargs)
    return densify(frame, **kwargs)


def _monthly_value_cols(
    kpi: KpiSpec, measure_keys: tuple[str, ...] = ()
) -> list[str]:
    """Columns densify must carry (named facts plus avg carry columns)."""
    from kpi_engine.capabilities.ops.support import monthly_fact_columns

    return monthly_fact_columns(kpi, measure_keys or None)


def _apply_host_defaults(
    request: AdaptedRequest, kpi: KpiSpec
) -> tuple[AdaptedRequest, KpiSpec]:
    """SelectedMetrics when the host omitted measures; then pick the request grain."""
    if request.measures_omitted and kpi.meta is not None and kpi.meta.selected_metrics:
        request = replace(request, measure_keys=kpi.meta.selected_metrics)
    request = replace(request, measure_keys=fold_measure_keys(kpi, request.measure_keys))
    assert_measure_keys(kpi, request.measure_keys)
    kpi = apply_request_time(kpi, declared_time_grain(kpi))
    requested, _needed = resolve_requested_graph(kpi, request.measure_keys)
    from kpi_engine.capabilities.ops.support import assert_partition_keys_for_request

    assert_partition_keys_for_request(kpi, requested)
    return replace(request, measure_keys=requested), kpi


def _stamp_green(rows: list[dict[str, Any]], kpi: KpiSpec) -> list[dict[str, Any]]:
    """Set row.green from green_when. Null value stays null."""
    rule = kpi.green_when
    if rule is None:
        return rows
    for row in rows:
        raw = row.get(rule.of)
        if raw is None:
            row["green"] = None
            continue
        try:
            if isinstance(raw, float) and raw != raw:
                row["green"] = None
                continue
            number = float(raw)
        except (TypeError, ValueError):
            row["green"] = None
            continue
        if rule.above is not None:
            row["green"] = number >= rule.above
        else:
            row["green"] = number <= float(rule.below)
    return rows


def _trend_labels(axes: dict[str, list[str]], kpi: KpiSpec) -> dict[str, list[str]]:
    """English chart labels aligned to ISO trend_axes."""
    return {
        key: [period_label(parse_date(item), kpi.time) for item in values]
        for key, values in axes.items()
    }


def _response_meta(kpi: KpiSpec) -> dict[str, Any] | None:
    """YAML meta plus green_when threshold, or None when both are absent."""
    if kpi.meta is None and kpi.green_when is None:
        return None
    out: dict[str, Any] = {}
    if kpi.meta is not None:
        out["KPI"] = kpi.meta.kpi
        out["ParentKPI"] = kpi.meta.parent_kpi
        out["IsChild"] = kpi.meta.is_child
        out["SelectedMetrics"] = list(kpi.meta.selected_metrics)
    if kpi.green_when is not None:
        out["of"] = kpi.green_when.of
        if kpi.green_when.above is not None:
            out["above"] = kpi.green_when.above
        else:
            out["below"] = kpi.green_when.below
    return out


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
    """Stable sort: output_cut, then request_grain, then remaining catalog names."""
    dim_order = list(kpi.request_grain) + [
        name for name in kpi.dimensions if name not in kpi.request_grain
    ]

    def key(row: dict[str, Any]) -> tuple:
        """Sort key for one result row."""
        return (str(row.get("output_cut") or ""), *[str(row.get(d) or "") for d in dim_order])

    return sorted(rows, key=key)


def _applied_cuts(kpi: KpiSpec, emitted: tuple) -> list[dict[str, Any]]:
    """Per-cut effective grain for the response."""
    return [
        {
            "name": cut.name,
            "group_by": list(effective_group_by(cut, kpi)),
            "exclude_from_grain": list(cut.exclude_from_grain),
        }
        for cut in emitted
    ]


def _grain_warnings(kpi: KpiSpec) -> list[dict[str, str]]:
    """Author-declared high-cardinality dims and explicit window/replace overrides."""
    high = {
        spec.name: spec
        for spec in kpi.dimension_specs
        if spec.cardinality == "high"
    }
    out: list[dict[str, str]] = []
    for name in kpi.request_grain:
        if name in high:
            out.append({"dimension": name, "reason": "high_cardinality"})
    for base in kpi.base_measures:
        if base.replace:
            out.append({"base": base.name, "reason": "replace_extract_column"})
        if base.agg_ok:
            out.append({"base": base.name, "reason": "window_agg_ok"})
    return out


def _dedupe_dropped(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the first drop reason per cut name."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        name = row.get("name") or ""
        if name in seen:
            continue
        seen.add(name)
        out.append(row)
    return out


def _assert_emitted_includes_default(
    kpi: KpiSpec, emitted: tuple, measure_keys: tuple[str, ...]
) -> None:
    """Default / locked cut must stay when this pipeline emits it; extras may drop."""
    names = {cut.name for cut in emitted}
    required: str | None = kpi.locked_cut
    if required is None:
        candidates = {cut.name for cut in cuts_for_keys(kpi, measure_keys)}
        if kpi.default_cut in candidates:
            required = kpi.default_cut
    if required is not None and required not in names:
        raise BindError(
            f"Cut {required!r} is not on this extract for the requested grain "
            f"{list(kpi.request_grain)}."
        )
    if not emitted:
        raise BindError(
            "No compatible cut for this extract. Check selected_dimensions "
            "against the model columns."
        )


def _assert_request_grain_on_models(
    kpi: KpiSpec,
    models: list[ModelSpec],
    available: set[str] | None,
    *,
    joined: bool = False,
    per_model: list[set[str] | None] | None = None,
) -> None:
    """Selected/default dims must exist on this extract (every model when joined)."""
    rename = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}

    def missing_from(cols: set[str] | None, name: str) -> bool:
        if cols is None:
            return False
        physical = rename.get(name, name)
        return match_name(name, cols) is None and match_name(physical, cols) is None

    for name in kpi.request_grain:
        if joined and per_model:
            for model, cols in zip(models, per_model):
                if missing_from(cols, name):
                    raise BindError(
                        f"selected_dimensions {name!r} is not on model {model.model_id!r}."
                    )
            continue
        if missing_from(available, name):
            model_ids = [m.model_id for m in models]
            raise BindError(
                f"selected_dimensions {name!r} is not on model(s) {model_ids}."
            )


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


def _applied(source_filters, deferred, result_filters) -> list[dict[str, Any]]:
    """Metadata: filters applied in DuckDB (extract), Pandas calc, or after cuts (result)."""
    rows = []
    for item in (*source_filters, *deferred, *result_filters):
        rows.append(
            {
                "filter_code": item.code,
                "column": item.column,
                "op": item.op,
                "values": list(item.values),
                "stage": item.stage,
                "apply": item.stage,
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
