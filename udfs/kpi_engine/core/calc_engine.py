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
from kpi_engine.core.cuts import cut_group_dims, effective_group_by
from kpi_engine.core.filters import apply_cut_filters
from kpi_engine.core.model_sql import NON_ADDITIVE
from kpi_engine.core.op_protocol import EvalCtx
from kpi_engine.core.op_registry import get_op
from kpi_engine.dates import period_range_inclusive
from kpi_engine.exceptions import CatalogError, KPIEngineError
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
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Evaluate requested measures at each cut; return JSON rows and shared trend axes."""
    measures = {m.key: m for m in kpi.measures}
    need = [k for k in requested if not get_op(measures[k].kind).echo_dimension]
    dim_keys = [k for k in requested if get_op(measures[k].kind).echo_dimension]
    rows: list[dict[str, Any]] = []
    trend_axes: dict[str, list[str]] = {}

    for cut in emitted:
        cut_monthly = _cut_monthly(monthly, cut, deferred_filters, kpi)
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
        _guard_trend_payload(len(combo_frame), trend_keys, measures, cut, kpi)

        cut_rows: list[dict[str, Any]] = []
        for _, combo in combo_frame.iterrows():
            series = _combo_series(
                cut_monthly, group_dims, combo, kpi.time.column if kpi.time else ""
            )
            memo: dict[str, Any] = {}
            row: dict[str, Any] = {
                "output_cut": cut.name,
                "grouped_dimensions": list(group_dims),
            }
            for dim in kpi.dimensions:
                if dim in group_dims:
                    row[dim] = _json_value(combo[dim]) if dim in combo.index else None
                else:
                    row[dim] = None
            for key in dim_keys:
                if key not in row:
                    row[key] = row.get(key)
            for key in need:
                spec = measures[key]
                plugin = get_op(spec.kind)
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
                    trend_axes[key] = axis
                    row[key] = values
                else:
                    row[key] = value
            cut_rows.append(row)
        for key in cut_phase_keys:
            get_op(measures[key].kind).apply_to_cut(cut_rows, measures[key], group_dims)
        rows.extend(cut_rows)
    return rows, trend_axes


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
) -> Any:
    """Dispatch one measure op against a single partition's monthly series.

    `memo` caches results for this partition so a measure named by several
    parents is computed once. Trends are not cached (they return an axis pair).
    """
    try:
        plugin = get_op(spec.kind)
    except CatalogError as exc:
        raise CatalogError(f"Cannot evaluate {spec.key} kind={spec.kind}.") from exc
    if memo is not None and spec.key in memo and not source_only:
        return memo[spec.key]

    def _child(child: OutputSpec) -> Any:
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
    )
    value = plugin.source_for_cut(ctx) if source_only else plugin.evaluate(ctx)
    if memo is not None and not plugin.emits_trend and not source_only:
        memo[spec.key] = value
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
) -> pd.DataFrame:
    """Filter then re-aggregate the monthly frame to this cut's group_by."""
    work = apply_cut_filters(monthly, cut, deferred)
    time_col = kpi.time.column if kpi.time is not None else None
    dims = list(cut_group_dims(cut, time_col or "", kpi))
    value_cols = [
        m.name
        for m in kpi.base_measures
        if m.agg not in NON_ADDITIVE or m.row_op
    ]
    extra = [f"{m.name}__sum" for m in kpi.base_measures if m.agg == "avg"]
    extra += [f"{m.name}__count" for m in kpi.base_measures if m.agg == "avg"]
    cols = [c for c in [*value_cols, *extra, "_observed"] if c in work.columns]
    if work.empty:
        return work
    if not cols:
        return work.iloc[0:0].copy()
    if time_col is not None and time_col not in work.columns:
        return work.iloc[0:0].copy()
    rollup = _rollup_funcs(kpi)
    agg: dict[str, str] = {}
    for col in cols:
        agg[col] = "max" if col == "_observed" else rollup.get(col, "sum")
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
) -> None:
    """Fail if row_count × trend length would exceed TREND_CELL_CAP."""
    grain = list(effective_group_by(cut, kpi))
    for key in trend_keys:
        length = catalog[key].trailing_months or 1
        cells = row_count * length
        if cells > TREND_CELL_CAP:
            raise KPIEngineError(
                f"Trend {key!r} on cut {cut.name!r} would emit {cells} cells "
                f"(cap {TREND_CELL_CAP}). Narrow selected_dimensions={list(kpi.request_grain)} "
                f"or measures.{key}.cuts. effective group_by={grain}."
            )


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
