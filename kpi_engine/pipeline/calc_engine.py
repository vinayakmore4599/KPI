"""Pandas catalog: dense spine, cuts, and plugin dispatch.

What this file provides
    densify — every partition × every month in the span (calendar spine).
    compute_cuts — one JSON row per dimension combo per cut.
    evaluate — build EvalCtx and call the registered OpPlugin.

Where it is used
    orchestrator after DuckDB extract. Tests in test_spine.py and
    test_cuts_reconcile.py.

Capabilities
    - Combo-phase plugins run per dimension combo; cut-phase plugins run after.
    - Trends, rank, and percent_of_total default to default_cut unless measures.*.cuts lists more.
    - row_set: span_union keeps combos seen anywhere in the span; anchor_only
      keeps only combos observed at the selected period.
    - Rows stamp only the cut's grain dimensions; unused catalog dims are omitted.

When to use
    Engine bugs (spine, cut loop, dispatch). New kinds go in capabilities/ops/
    plus registries/ops.yaml — not here.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.capabilities.ops.support import cut_limited_applies
from kpi_engine.contracts import (
    BoundFilter,
    CutSpec,
    KpiSpec,
    OutputSpec,
    TimePlan,
)
from kpi_engine.pipeline.cuts import cut_group_dims, effective_group_by
from kpi_engine.pipeline.filters import apply_cut_filters
from kpi_engine.pipeline.model_sql import NON_ADDITIVE
from kpi_engine.pipeline.op_protocol import EvalCtx
from kpi_engine.pipeline.op_registry import get_op
from kpi_engine.dates import period_range_inclusive
from kpi_engine.exceptions import BindError, CalcError, CatalogError, KPIEngineError
from kpi_engine.identifiers import match_name
from kpi_engine.runlog import traced

TREND_CELL_CAP = 50_000


@traced
def densify(
    frame: pd.DataFrame,
    *,
    keys: list[str],
    time_col: str,
    start: date,
    end: date,
    value_cols: list[str],
    fill_zero_cols: list[str],
    time_spec: Any | None = None,
    kpi: KpiSpec | None = None,
    grain: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Fill every partition with every period in [start, end] so shifts move by calendar."""
    from kpi_engine.dates import month_range_inclusive, parse_month

    work = frame.copy()
    work[time_col] = pd.to_datetime(work[time_col]).dt.normalize()
    resolved: list[str] = []
    seen_keys: set[str] = set()
    for key in keys:
        actual = key if key in work.columns else match_name(key, work.columns)
        if actual is None or actual in seen_keys:
            continue
        resolved.append(actual)
        seen_keys.add(actual)
    keys = resolved
    if time_spec is None:
        months = pd.to_datetime(month_range_inclusive(parse_month(start), parse_month(end)))
    else:
        months = pd.to_datetime(period_range_inclusive(start, end, time_spec))
    if keys:
        groups = work[keys].drop_duplicates()
        n_combos = len(groups)
    else:
        groups = None
        n_combos = 1
    n_periods = len(months)
    if n_combos * n_periods > TREND_CELL_CAP:
        selected = list(kpi.request_grain) if kpi is not None else list(keys)
        extract = list(grain) if grain is not None else list(keys)
        raise CatalogError(
            f"Densify grid {n_combos} combos × {n_periods} periods exceeds "
            f"{TREND_CELL_CAP}. selected_dimensions={selected} extract grain={extract}."
        )
    if keys:
        grid = groups.merge(pd.DataFrame({time_col: months}), how="cross")
    else:
        grid = pd.DataFrame({time_col: months})
    merged = grid.merge(work, on=[*keys, time_col], how="left")
    observed = pd.Series(False, index=merged.index)
    for col in value_cols:
        if col in merged.columns:
            observed = observed | merged[col].notna()
    merged["_observed"] = observed
    last_observed = None
    if not work.empty and time_col in work.columns:
        last_observed = work[time_col].max()
    for col in fill_zero_cols:
        if col not in merged.columns:
            continue
        fill = ~merged["_observed"]
        if last_observed is not None:
            fill = fill & (merged[time_col] <= last_observed)
        merged.loc[fill, col] = merged.loc[fill, col].fillna(0)
    return merged


@traced
def compute_cuts(
    monthly: pd.DataFrame,
    *,
    kpi: KpiSpec,
    emitted: tuple[CutSpec, ...],
    deferred_filters: tuple[BoundFilter, ...],
    plan: TimePlan | None,
    requested: tuple[str, ...],
    detail: pd.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[dict[str, Any]]]:
    """Evaluate requested measures at each cut; return JSON rows, trend axes, dropped_groups."""
    measures = {m.key: m for m in kpi.measures}
    requested_keys = [k for k in requested if k in measures]
    need = [k for k in requested_keys if not get_op(measures[k].kind).echo_dimension]
    dim_keys = [k for k in requested_keys if get_op(measures[k].kind).echo_dimension]
    hidden = _combo_inputs_for(need, measures)
    eval_need = list(dict.fromkeys([*hidden, *need]))
    rows: list[dict[str, Any]] = []
    trend_axes: dict[str, list[str]] = {}
    dropped_groups: list[dict[str, Any]] = []
    totals: dict[tuple[str, str], float] = {}

    versus_targets = {measures[k].versus_cut for k in eval_need if measures[k].versus_cut}
    from_cut_targets = {measures[k].from_cut for k in eval_need if measures[k].from_cut}
    by_cut = {c.name: c for c in kpi.cuts}
    emitted_names = {c.name for c in emitted}
    silent_names = (versus_targets | from_cut_targets) - emitted_names
    from_cut_store: dict[tuple[str, str], list[dict[str, Any]]] = {}
    monthly_cache: dict[str, pd.DataFrame] = {}

    def monthly_for(cut: CutSpec) -> pd.DataFrame:
        if cut.name in monthly_cache:
            return monthly_cache[cut.name]
        if cut.rollup_from and cut.rollup_from in by_cut:
            child_m = monthly_for(by_cut[cut.rollup_from])
            override = list(cut.rollup_dims) if cut.rollup_dims else None
            result = _cut_monthly(
                child_m, cut, deferred_filters, kpi, group_dims_override=override
            )
        else:
            result = _cut_monthly(monthly, cut, deferred_filters, kpi)
        monthly_cache[cut.name] = result
        return result

    def remember_from_cut(cut_name: str, group_dims: list[str], cut_rows: list) -> None:
        for key in eval_need:
            if measures[key].from_cut != cut_name:
                continue
            from_cut_store[(cut_name, key)] = [
                {**{dim: row.get(dim) for dim in group_dims}, key: row.get(key)}
                for row in cut_rows
            ]

    def overlay_from_cut(cut: CutSpec, group_dims: list[str], cut_rows: list) -> None:
        for key in eval_need:
            spec = measures[key]
            if not spec.from_cut:
                continue
            src_rows = from_cut_store.get((spec.from_cut, key), [])
            src = by_cut.get(spec.from_cut)
            if src is None:
                continue
            src_dims = list(cut_group_dims(src, kpi.time.column if kpi.time else "", kpi))
            shared = [dim for dim in src_dims if dim in group_dims]
            if not shared:
                value = src_rows[0].get(key) if src_rows else None
                for row in cut_rows:
                    row[key] = value
                continue
            index = {
                tuple(row.get(dim) for dim in shared): row.get(key) for row in src_rows
            }
            for row in cut_rows:
                row[key] = index.get(tuple(row.get(dim) for dim in shared))

    for name in silent_names:
        if name not in by_cut:
            continue
        silent = by_cut[name]
        silent_monthly = monthly_for(silent)
        silent_detail = (
            apply_cut_filters(detail, silent, deferred_filters)
            if detail is not None
            else None
        )
        silent_dims = list(cut_group_dims(silent, kpi.time.column if kpi.time else "", kpi))
        if silent_monthly.empty:
            combo_frame = pd.DataFrame([{}]) if not silent_dims else pd.DataFrame()
        else:
            combo_frame = (
                silent_monthly[silent_dims].drop_duplicates()
                if silent_dims
                else pd.DataFrame([{}])
            )
        of_keys = sorted(
            {
                measures[k].of
                for k in eval_need
                if measures[k].versus_cut == name and measures[k].of
            }
        )
        extra_vs = [
            measures[k].params.get("vs")
            for k in eval_need
            if measures[k].versus_cut == name and measures[k].kind == "contribution"
        ]
        from_keys = sorted(
            k for k in eval_need if measures[k].from_cut == name
        )
        silent_need = list(
            dict.fromkeys(
                [*of_keys, *[v for v in extra_vs if v], *from_keys]
            )
        )
        silent_rows = _evaluate_combos(
            combo_frame,
            silent_monthly,
            silent_detail,
            silent,
            silent_dims,
            kpi,
            plan,
            measures,
            silent_need,
            [],
            {},
        )
        _store_versus_totals(silent_rows, name, eval_need, measures, totals)
        remember_from_cut(name, silent_dims, silent_rows)

    ordered = _order_cuts_by_versus(emitted, eval_need, measures)

    for cut in ordered:
        cut_monthly = monthly_for(cut)
        cut_detail = apply_cut_filters(detail, cut, deferred_filters) if detail is not None else None
        group_dims = list(cut_group_dims(cut, kpi.time.column if kpi.time else "", kpi))
        if cut_monthly.empty and not group_dims:
            combo_frame = pd.DataFrame([{}])
        elif cut_monthly.empty and cut_detail is not None and not cut_detail.empty:
            combo_frame = (
                cut_detail[group_dims].drop_duplicates() if group_dims else pd.DataFrame([{}])
            )
        elif cut_monthly.empty:
            continue
        else:
            combo_frame = cut_monthly[group_dims].drop_duplicates() if group_dims else pd.DataFrame([{}])

        if kpi.row_set == "anchor_only":
            combo_frame = _combos_at_anchor(cut_monthly, group_dims, kpi, plan)
            if combo_frame.empty:
                continue

        trend_keys, cut_phase_keys = _phase_keys(need, measures, cut, kpi)
        _guard_trend_payload(len(combo_frame), trend_keys, measures, cut, kpi, plan)

        cut_rows = _evaluate_combos(
            combo_frame,
            cut_monthly,
            cut_detail,
            cut,
            group_dims,
            kpi,
            plan,
            measures,
            eval_need,
            dim_keys,
            trend_axes,
            axis_keys=set(need),
        )
        if kpi.having is not None:
            kept, dropped = _apply_having(cut_rows, kpi, cut, group_dims)
            dropped_groups.extend(dropped)
            from kpi_engine.pipeline.row_pipeline import OVER_PARTITION_CAP

            if len(dropped_groups) > OVER_PARTITION_CAP:
                raise CatalogError(
                    f"dropped_groups has {len(dropped_groups)} entries, cap "
                    f"{OVER_PARTITION_CAP}."
                )
            cut_rows = kept
            if kpi.having.then_group_by is not None:
                cut_rows, extra_axes = _rollup_after_having(
                    cut_rows,
                    cut,
                    group_dims,
                    cut_monthly,
                    cut_detail,
                    kpi,
                    plan,
                    measures,
                    eval_need,
                    dim_keys,
                    deferred_filters,
                )
                trend_axes.update(extra_axes)
                group_dims = list(kpi.having.then_group_by)
                trend_keys, cut_phase_keys = _phase_keys(need, measures, cut, kpi)
        _store_versus_totals(cut_rows, cut.name, eval_need, measures, totals)
        remember_from_cut(cut.name, group_dims, cut_rows)
        overlay_from_cut(cut, group_dims, cut_rows)
        for key in cut_phase_keys:
            spec = measures[key]
            if spec.kind == "percent_of_total":
                src = f"__cut_src_{key}"
                values = [
                    v
                    for v in (_numeric_row(r.get(src)) for r in cut_rows)
                    if v is not None
                ]
                totals[(cut.name, spec.of or spec.key)] = float(sum(values))
            get_op(spec.kind).apply_to_cut(
                cut_rows, spec, group_dims, totals=totals
            )
        _apply_cut_derived(cut_rows, eval_need, measures)
        _apply_measure_having(cut_rows, eval_need, measures)
        if hidden:
            for row in cut_rows:
                for key in hidden:
                    row.pop(key, None)
        rows.extend(cut_rows)
    if kpi.omit_null_rows:
        rows = _omit_null_rows(rows, requested_keys, measures)
    return rows, trend_axes, dropped_groups


def _phase_keys(need, measures, cut, kpi):
    trend_keys = [
        k
        for k in need
        if get_op(measures[k].kind).emits_trend
        and (
            not get_op(measures[k].kind).cut_restricted
            or cut_limited_applies(measures[k], cut, kpi)
        )
    ]
    cut_phase_keys = [
        k
        for k in need
        if get_op(measures[k].kind).phase == "cut"
        and (
            not get_op(measures[k].kind).cut_restricted
            or cut_limited_applies(measures[k], cut, kpi)
        )
    ]
    return trend_keys, cut_phase_keys


def _evaluate_combos(
    combo_frame: pd.DataFrame,
    cut_monthly: pd.DataFrame,
    cut_detail: pd.DataFrame | None,
    cut: CutSpec,
    group_dims: list[str],
    kpi: KpiSpec,
    plan: TimePlan | None,
    measures: dict[str, OutputSpec],
    need: list[str],
    dim_keys: list[str],
    trend_axes: dict[str, list[str]],
    axis_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    cut_rows: list[dict[str, Any]] = []
    time_col = kpi.time.column if kpi.time else ""
    _ = dim_keys
    for _, combo in combo_frame.iterrows():
        series = _combo_series(cut_monthly, group_dims, combo, time_col)
        memo: dict[str, Any] = {}
        row: dict[str, Any] = {
            "output_cut": cut.name,
            "grouped_dimensions": list(group_dims),
        }
        for dim in group_dims:
            if dim not in combo.index:
                raise CalcError(
                    f"Cut {cut.name!r} grain dimension {dim!r} is missing from the combo."
                )
            row[dim] = _json_value(combo[dim])
        for key in need:
            spec = measures[key]
            plugin = get_op(spec.kind)
            if spec.cut_derived:
                continue
            if plugin.cut_restricted and not cut_limited_applies(spec, cut, kpi):
                continue
            if plugin.phase == "cut":
                row[f"__cut_src_{key}"] = evaluate(
                    spec,
                    series,
                    kpi,
                    plan,
                    measures,
                    detail=cut_detail,
                    combo=combo,
                    group_dims=group_dims,
                    memo=memo,
                    cut=cut.name,
                    source_only=True,
                )
                continue
            value = evaluate(
                spec,
                series,
                kpi,
                plan,
                measures,
                detail=cut_detail,
                combo=combo,
                group_dims=group_dims,
                memo=memo,
                cut=cut.name,
            )
            if plugin.emits_trend:
                axis, values = value
                if axis_keys is None or key in axis_keys:
                    trend_axes[key] = axis
                row[key] = values
            else:
                row[key] = value
        cut_rows.append(row)
    return cut_rows


def _apply_having(
    cut_rows: list[dict[str, Any]],
    kpi: KpiSpec,
    cut: CutSpec,
    group_dims: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from kpi_engine.pipeline.predicates import eval_predicate_list

    having = kpi.having
    if having is None:
        return cut_rows, []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in cut_rows:
        if eval_predicate_list(having.predicates, having.match, row):
            kept.append(row)
            continue
        dropped.append(
            {
                "cut": cut.name,
                "reason": "having",
                "key": {dim: row.get(dim) for dim in group_dims},
            }
        )
    return kept, dropped


def _rollup_after_having(
    cut_rows: list[dict[str, Any]],
    cut: CutSpec,
    group_dims: list[str],
    cut_monthly: pd.DataFrame,
    cut_detail: pd.DataFrame | None,
    kpi: KpiSpec,
    plan: TimePlan | None,
    measures: dict[str, OutputSpec],
    need: list[str],
    dim_keys: list[str],
    deferred_filters: tuple[BoundFilter, ...],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Re-fold surviving facts to then_group_by, then re-run combo measures."""
    from kpi_engine.pipeline.fn_apply import collapse_pandas_detail, pandas_group_keys
    from kpi_engine.dates import add_periods

    having = kpi.having
    if having is None or having.then_group_by is None:
        return cut_rows, {}
    target = list(having.then_group_by)
    missing = [d for d in target if d not in group_dims]
    if missing:
        raise BindError(
            f"having.then_group_by {missing} is not on cut {cut.name!r} "
            f"(effective grain {group_dims})."
        )
    if not cut_rows:
        return [], {}
    keys_df = pd.DataFrame([{dim: row.get(dim) for dim in group_dims} for row in cut_rows])
    keys_df = keys_df.drop_duplicates()
    if cut_detail is not None and not cut_detail.empty:
        present = [c for c in group_dims if c in cut_detail.columns]
        survivors = (
            cut_detail.merge(keys_df[present], on=present, how="inner")
            if present and not keys_df.empty
            else cut_detail.iloc[0:0]
        )
    else:
        present = [c for c in group_dims if c in cut_monthly.columns]
        survivors = (
            cut_monthly.merge(keys_df[present], on=present, how="inner")
            if present and not keys_df.empty
            else cut_monthly.iloc[0:0]
        )
    grain = tuple(target)
    collapsed = collapse_pandas_detail(
        survivors, kpi, grain, facts_applied=True, measure_keys=tuple(need)
    )
    if kpi.time is None or plan is None:
        rolled = collapsed.copy()
        if "_observed" not in rolled.columns:
            rolled["_observed"] = True
    else:
        from kpi_engine.capabilities.ops.support import monthly_fact_columns

        time_col = kpi.time.column
        keys = pandas_group_keys(kpi, grain)
        value_cols = monthly_fact_columns(kpi, tuple(need))
        fill_zero = [
            m.name
            for m in kpi.base_measures
            if m.agg in {"sum", "count"} and m.where is None and not m.also_where
        ]
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
        if collapsed.empty:
            rolled = densify(
                pd.DataFrame(columns=[time_col, *keys, *value_cols]), **kwargs
            )
        else:
            rolled = densify(collapsed, **kwargs)
    combo_frame = (
        rolled[target].drop_duplicates() if target and not rolled.empty
        else (pd.DataFrame([{}]) if not target else rolled.iloc[0:0])
    )
    extra_axes: dict[str, list[str]] = {}
    new_rows = _evaluate_combos(
        combo_frame,
        rolled,
        survivors,
        cut,
        target,
        kpi,
        plan,
        measures,
        need,
        dim_keys,
        extra_axes,
        axis_keys=set(need),
    )
    return new_rows, extra_axes


def evaluate(
    spec: OutputSpec,
    series: pd.DataFrame,
    kpi: KpiSpec,
    plan: TimePlan | None,
    catalog: dict[str, OutputSpec],
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
    memo: dict[str, Any] | None = None,
    cut: str = "",
    source_only: bool = False,
    anchor: date | None = None,
    selection: tuple[date, ...] | None = None,
) -> Any:
    """Dispatch one measure op against a single partition's monthly series.

    `memo` is keyed by (spec.key, effective_anchor) so a parent can hold the
    same child at two periods in one combo pass. `source_only` is never cached.
    `_child` inherits this call's effective anchor unless `anchor=` is passed.
    `selection=` overrides the request-level time selection for this eval.
    """
    try:
        plugin = get_op(spec.kind)
    except CatalogError as exc:
        raise CatalogError(f"Cannot evaluate {spec.key} kind={spec.kind}.") from exc
    inherited_selection = selection
    if anchor is not None:
        effective = anchor
    elif inherited_selection:
        effective = inherited_selection[-1]
    else:
        effective = plan.anchor if plan else None
    memo_key = (spec.key, effective, inherited_selection)
    if memo is not None and memo_key in memo and not source_only:
        return memo[memo_key]

    def _child(child: OutputSpec, *, anchor: date | None = None, selection: tuple[date, ...] | None = None) -> Any:
        next_selection = selection if selection is not None else inherited_selection
        if anchor is not None:
            next_anchor = anchor
        elif selection is not None:
            next_anchor = None
        else:
            next_anchor = effective
        return evaluate(
            child,
            series,
            kpi,
            plan,
            catalog,
            detail,
            combo,
            group_dims,
            memo=memo,
            cut=cut,
            anchor=next_anchor,
            selection=next_selection,
        )

    if (
        effective is not None
        and plan is not None
        and effective != plan.anchor
        and not plugin.shiftable
    ):
        raise CatalogError(
            f"{spec.key} op={spec.kind} cannot be evaluated at a shifted anchor."
        )

    ctx = EvalCtx(
        spec=spec,
        series=series,
        kpi=kpi,
        plan=plan,
        catalog=catalog,
        detail=detail,
        combo=combo,
        group_dims=list(group_dims or []),
        memo=memo if memo is not None else {},
        cut=cut,
        evaluate=_child,
        anchor=effective,
        selection=inherited_selection,
    )
    value = plugin.source_for_cut(ctx) if source_only else plugin.evaluate(ctx)
    if memo is not None and not plugin.emits_trend and not source_only:
        memo[memo_key] = value
    return value


def _combos_at_anchor(
    cut_monthly: pd.DataFrame,
    group_dims: list[str],
    kpi: KpiSpec,
    plan: TimePlan | None,
) -> pd.DataFrame:
    """Keep only dimension combos that have observed activity at the anchor period."""
    empty = pd.DataFrame(columns=group_dims) if group_dims else pd.DataFrame()
    if kpi.time is None or plan is None:
        if cut_monthly.empty:
            return empty
        if not group_dims:
            return pd.DataFrame([{}])
        return cut_monthly[group_dims].drop_duplicates()
    if cut_monthly.empty or kpi.time.column not in cut_monthly.columns:
        return empty
    ts = pd.to_datetime(cut_monthly[kpi.time.column])
    if plan.anchor is None:
        return empty
    at = cut_monthly.loc[ts == pd.Timestamp(plan.anchor)]
    if "_observed" in at.columns:
        at = at.loc[at["_observed"].astype(bool)]
    if at.empty:
        return empty
    if not group_dims:
        return pd.DataFrame([{}])
    return at[group_dims].drop_duplicates()


def _cut_monthly(
    monthly: pd.DataFrame,
    cut: CutSpec,
    deferred: tuple[BoundFilter, ...],
    kpi: KpiSpec,
    group_dims_override: list[str] | None = None,
) -> pd.DataFrame:
    """Filter then re-aggregate the monthly frame to this cut's group_by."""
    from kpi_engine.capabilities.ops.support import monthly_fact_columns

    work = apply_cut_filters(monthly, cut, deferred)
    time_col = kpi.time.column if kpi.time is not None else None
    dims = (
        list(group_dims_override)
        if group_dims_override is not None
        else list(cut_group_dims(cut, time_col or "", kpi))
    )
    value_cols = monthly_fact_columns(kpi)
    extra = [f"{m.name}__sum" for m in kpi.base_measures if m.agg == "avg"]
    extra += [f"{m.name}__count" for m in kpi.base_measures if m.agg == "avg"]
    extra += [f"{m.name}__wsum" for m in kpi.base_measures if m.agg == "weighted_avg"]
    extra += [f"{m.name}__wcount" for m in kpi.base_measures if m.agg == "weighted_avg"]
    cols = [c for c in [*value_cols, *extra, "_observed"] if c in work.columns]
    if work.empty:
        return work
    if not cols:
        return work.iloc[0:0].copy()
    if time_col is not None and time_col not in work.columns:
        return work.iloc[0:0].copy()
    rollup = _rollup_funcs(kpi)
    where_cols = {
        m.name
        for m in kpi.base_measures
        if m.where is not None or m.also_where
    }

    def _sum_or_null_col(series: pd.Series):
        return series.sum(min_count=1)

    agg: dict[str, Any] = {}
    for col in cols:
        if col == "_observed":
            agg[col] = "max"
        elif col in where_cols and rollup.get(col, "sum") == "sum":
            agg[col] = _sum_or_null_col
        else:
            agg[col] = rollup.get(col, "sum")
    group = [*dims, time_col] if time_col is not None else list(dims)
    if not group:
        out = work[list(agg)].agg(agg)
        out = out.to_frame().T if isinstance(out, pd.Series) else out.reset_index(drop=True)
    else:
        out = work.groupby(group, dropna=False, as_index=False).agg(agg)
    out["_observed"] = out["_observed"].astype(bool)
    return out


def _rollup_funcs(kpi: KpiSpec) -> dict[str, str]:
    """How each measure column combines across partitions of a coarser cut.

    Summing a MIN across regions would report the sum of regional minima, so
    min/max keep their own function. avg is carried as __sum and __count, which
    do add up.
    """
    funcs: dict[str, str] = {}
    for measure in kpi.base_measures:
        if measure.agg in {"min", "max", "first", "last"}:
            funcs[measure.name] = measure.agg
    return funcs


def _combo_series(
    cut_monthly: pd.DataFrame,
    group_dims: list[str],
    combo: pd.Series,
    time_col: str,
) -> pd.DataFrame:
    """Rows for one dimension combination, ordered by time."""
    work = cut_monthly
    if work.empty:
        return work
    for dim in group_dims:
        if dim not in work.columns:
            continue
        work = work[work[dim] == combo[dim]]
    return work.sort_values(time_col) if time_col in work.columns else work


def _guard_trend_payload(
    row_count: int,
    trend_keys: list[str],
    catalog: dict[str, OutputSpec],
    cut: CutSpec,
    kpi: KpiSpec,
    plan: TimePlan | None,
) -> None:
    """Fail if row_count × actual trend axis length would exceed TREND_CELL_CAP."""
    grain = list(effective_group_by(cut, kpi))
    for key in trend_keys:
        spec = catalog[key]
        length = _trend_axis_length(spec, kpi, plan)
        cells = row_count * length
        if cells > TREND_CELL_CAP:
            raise KPIEngineError(
                f"Trend {key!r} on cut {cut.name!r} would emit {cells} cells "
                f"(exceeds cap {TREND_CELL_CAP}). Narrow selected_dimensions={list(kpi.request_grain)} "
                f"or measures.{key}.cuts. effective group_by={grain}."
            )


def _trend_axis_length(spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None) -> int:
    """Period count from periods(), not trailing_months-or-1."""
    plugin = get_op(spec.kind)
    try:
        meta = plugin.periods(spec, kpi, plan)
    except BindError:
        meta = None
    if meta and meta.get("periods"):
        return max(len(meta["periods"]), 1)
    return spec.trailing_months or 1


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy scalars to plain Python (None for NA)."""
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def _apply_measure_having(
    cut_rows: list[dict[str, Any]],
    eval_need: list[str],
    measures: dict[str, OutputSpec],
) -> None:
    """Null a measure when its having: predicate fails. Does not drop the row."""
    from kpi_engine.pipeline.predicates import eval_predicate_list

    for key in eval_need:
        spec = measures.get(key)
        if spec is None or spec.having is None:
            continue
        for row in cut_rows:
            if not eval_predicate_list(spec.having.predicates, spec.having.match, row):
                row[key] = None


def _omit_null_rows(
    rows: list[dict[str, Any]], requested: list[str], measures: dict[str, OutputSpec]
) -> list[dict[str, Any]]:
    """Drop rows where every requested scalar is JSON null (0/false stay)."""
    scalar_keys = [
        key
        for key in requested
        if key in measures and not get_op(measures[key].kind).emits_trend
    ]
    check = scalar_keys if scalar_keys else [
        key for key in requested if key in measures
    ]
    if not check:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        if all(_cell_is_json_null(row.get(key)) for key in check):
            continue
        kept.append(row)
    return kept


def _cell_is_json_null(cell: Any) -> bool:
    if cell is None:
        return True
    try:
        if pd.isna(cell):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(cell, dict) and "value" in cell:
        return _cell_is_json_null(cell.get("value"))
    if isinstance(cell, list):
        if not cell:
            return True
        return all(_cell_is_json_null(item) for item in cell)
    return False


def _numeric_row(value: Any) -> float | None:
    """Coerce a result-row cell to float, or None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _combo_inputs_for(need, measures) -> list[str]:
    """Combo-phase measure keys that requested cut_derived / cut ops need on the row."""
    extra: list[str] = []
    seen = set(need)

    def walk(key: str) -> None:
        spec = measures.get(key)
        if spec is None:
            return
        for dep in get_op(spec.kind).dependencies(spec):
            child = measures.get(dep)
            if child is None or dep in seen:
                continue
            if get_op(child.kind).echo_dimension:
                continue
            seen.add(dep)
            extra.append(dep)
            walk(dep)

    for key in list(need):
        walk(key)
    return extra


def _store_versus_totals(cut_rows, cut_name, need, measures, totals) -> None:
    """Record this cut's of-measure totals for versus_cut consumers."""
    of_keys = {
        measures[k].of
        for k in need
        if getattr(measures.get(k), "versus_cut", None) == cut_name and measures[k].of
    }
    for of_key in of_keys:
        values = [
            v for v in (_numeric_row(r.get(of_key)) for r in cut_rows) if v is not None
        ]
        totals[(cut_name, of_key)] = float(sum(values))
    for key in need:
        spec = measures.get(key)
        if spec is None or spec.versus_cut != cut_name or spec.kind != "contribution":
            continue
        vs = spec.params.get("vs")
        deltas: list[float] = []
        for row in cut_rows:
            current = _numeric_row(row.get(spec.of))
            baseline = _numeric_row(row.get(vs))
            if current is not None and baseline is not None:
                deltas.append(float(current) - float(baseline))
        totals[(cut_name, f"__contrib_{spec.of}_{vs}")] = float(sum(deltas))


def _order_cuts_by_versus(emitted, need, measures) -> list:
    """Emit versus_cut / from_cut sources before consumers. Cycles keep original order."""
    targets = set()
    for key in need:
        spec = measures[key]
        if spec.versus_cut:
            targets.add(spec.versus_cut)
        if spec.from_cut:
            targets.add(spec.from_cut)
    first = [c for c in emitted if c.name in targets]
    rest = [c for c in emitted if c.name not in targets]
    return first + rest


def _apply_cut_derived(cut_rows, need, measures) -> None:
    """Fill arithmetic/fn/expr that consume cut-phase ops from finished row values."""
    derived = [k for k in need if measures[k].cut_derived]
    if not derived:
        return
    from kpi_engine.pipeline.fn_apply import call_measure_fn, eval_expr_scalar

    pending = list(derived)
    seen: set[str] = set()
    while pending:
        progress = False
        leftover: list[str] = []
        for key in pending:
            spec = measures[key]
            deps = [n for n in _cut_derived_deps(spec) if n in measures]
            if any(d in pending or (measures[d].cut_derived and d not in seen) for d in deps):
                leftover.append(key)
                continue
            for row in cut_rows:
                row[key] = _eval_cut_derived_row(row, spec, call_measure_fn, eval_expr_scalar)
            seen.add(key)
            progress = True
        if not progress:
            for key in leftover:
                spec = measures[key]
                for row in cut_rows:
                    row[key] = _eval_cut_derived_row(
                        row, spec, call_measure_fn, eval_expr_scalar
                    )
            break
        pending = leftover


def _cut_derived_deps(spec: OutputSpec) -> tuple[str, ...]:
    if spec.kind in {"fn", "expr"}:
        return spec.inputs
    if spec.operands:
        return spec.operands
    return tuple(n for n in (spec.left, spec.right) if n)


def _eval_cut_derived_row(row, spec, call_measure_fn, eval_expr_scalar):
    names = list(_cut_derived_deps(spec))
    values = [row.get(n) for n in names]
    if spec.kind == "expr" and spec.expr:
        env = {n: row.get(n) for n in names}
        from kpi_engine.identifiers import parse_expression

        return eval_expr_scalar(parse_expression(spec.expr, what="measure expr"), env)
    fn = spec.fn or "divide"
    return call_measure_fn(fn, values, spec.input_params)
