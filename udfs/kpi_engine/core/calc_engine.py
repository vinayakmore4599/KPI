"""Pandas catalog: dense spine, cuts, point/window/trend/arithmetic.

What this file provides
    densify — every partition × every month in the span (calendar spine).
    compute_cuts — one JSON row per dimension combo per cut.
    evaluate — dispatch measure ops (no eval, no importlib).

Where it is used
    orchestrator after DuckDB extract. Tests in test_spine.py and
    test_cuts_reconcile.py.

Capabilities
    - Point: value at anchor ± calendar offset; unobserved month → null.
    - Window: trailing N months (inclusive by default) using declared agg.
    - Trend: fixed-length array + shared axis in metadata (graphs).
    - Arithmetic / fn / expr: compose other measures; /0 and /null → null.
    - Hook: allowlisted function from extensions.hooks.REGISTRY.
    - Trends default to default_cut only unless measures.*.cuts lists more.
    - row_set: span_union keeps combos seen anywhere in the span; anchor_only
      keeps only combos observed at the selected period.

When to use
    Add a new op kind here (and in binder._parse_measure). KPI YAML should
    reuse existing ops via `of` / `op` rather than custom Python.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.catalog.ops_impl import call_measure_fn, eval_expr_scalar
from kpi_engine.contracts import (
    BoundFilter,
    CutSpec,
    KpiSpec,
    OutputSpec,
    TimePlan,
)
from kpi_engine.core.cuts import cut_group_dims
from kpi_engine.core.filters import apply_cut_filters
from kpi_engine.core.model_sql import NON_ADDITIVE
from kpi_engine.dates import add_periods, iso_period, period_range_inclusive
from kpi_engine.exceptions import CatalogError, KPIEngineError
from kpi_engine.identifiers import norm_name
from kpi_engine.runlog import log_measure, traced

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
) -> pd.DataFrame:
    """Fill every partition with every period in [start, end] so shifts move by calendar."""
    from kpi_engine.dates import month_range_inclusive, parse_month

    work = frame.copy()
    work[time_col] = pd.to_datetime(work[time_col]).dt.normalize()
    if time_spec is None:
        months = pd.to_datetime(month_range_inclusive(parse_month(start), parse_month(end)))
    else:
        months = pd.to_datetime(period_range_inclusive(start, end, time_spec))
    if keys:
        groups = work[keys].drop_duplicates()
        grid = groups.merge(pd.DataFrame({time_col: months}), how="cross")
    else:
        grid = pd.DataFrame({time_col: months})
    merged = grid.merge(work, on=[*keys, time_col], how="left")
    observed = pd.Series(False, index=merged.index)
    for col in value_cols:
        if col in merged.columns:
            observed = observed | merged[col].notna()
    merged["_observed"] = observed
    for col in fill_zero_cols:
        if col in merged.columns:
            merged.loc[~merged["_observed"], col] = merged.loc[~merged["_observed"], col].fillna(0)
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
    need = [k for k in requested if measures[k].kind != "dimension"]
    dim_keys = [k for k in requested if measures[k].kind == "dimension"]
    rows: list[dict[str, Any]] = []
    trend_axes: dict[str, list[str]] = {}

    for cut in emitted:
        cut_monthly = _cut_monthly(monthly, cut, deferred_filters, kpi)
        cut_detail = apply_cut_filters(detail, cut, deferred_filters) if detail is not None else None
        group_dims = list(cut_group_dims(cut, kpi.time.column if kpi.time else ""))
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
            if measures[k].kind == "trend" and _cut_limited_applies(measures[k], cut, kpi)
        ]
        rank_keys = [
            k
            for k in need
            if measures[k].kind == "rank" and _cut_limited_applies(measures[k], cut, kpi)
        ]
        _guard_trend_payload(len(combo_frame), trend_keys, measures, cut)

        cut_rows: list[dict[str, Any]] = []
        for _, combo in combo_frame.iterrows():
            series = _combo_series(
                cut_monthly, group_dims, combo, kpi.time.column if kpi.time else ""
            )
            memo: dict[str, Any] = {}
            row: dict[str, Any] = {"output_cut": cut.name}
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
                if spec.kind in {"trend", "rank"} and not _cut_limited_applies(spec, cut, kpi):
                    continue
                if spec.kind == "rank":
                    row[f"__rank_src_{key}"] = _rank_source(
                        spec,
                        series,
                        kpi,
                        plan,
                        measures,
                        cut_detail,
                        combo,
                        group_dims,
                        memo,
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
                )
                if spec.kind == "trend":
                    axis, values = value
                    trend_axes[key] = axis
                    row[key] = values
                else:
                    row[key] = value
                combo_vals = {
                    dim: _json_value(combo[dim]) if dim in combo.index else None
                    for dim in group_dims
                }
                log_measure(cut.name, key, spec.kind, combo_vals, row[key])
            cut_rows.append(row)
        _apply_ranks(cut_rows, rank_keys, measures, group_dims)
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
) -> Any:
    """Dispatch one measure op against a single partition's monthly series.

    `memo` caches results for this partition so a measure named by several
    parents is computed once. Trends are not cached (they return an axis pair).
    """
    if memo is not None and spec.key in memo:
        return memo[spec.key]
    value = _evaluate_uncached(
        spec, series, kpi, plan, catalog, detail, combo, group_dims, memo
    )
    if memo is not None and spec.kind != "trend":
        memo[spec.key] = value
    return value


def _evaluate_uncached(
    spec: OutputSpec,
    series: pd.DataFrame,
    kpi: KpiSpec,
    plan: TimePlan | None,
    catalog: dict[str, OutputSpec],
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
    memo: dict[str, Any] | None = None,
) -> Any:
    """Run one measure op. Callers should use evaluate so results are memoized."""
    if spec.kind == "point":
        if kpi.time is None or plan is None:
            return _point(series, kpi, spec.of, target=None)
        offset = spec.offset
        target = truncate_period_safe(plan.anchor, kpi)
        if offset:
            from dataclasses import replace
            from kpi_engine.dates import apply_offset, truncate_period

            lookback = replace(
                offset,
                days=-offset.days,
                months=-offset.months,
                years=-offset.years,
                quarters=-offset.quarters,
            )
            target = truncate_period(apply_offset(plan.anchor, lookback), kpi.time)
        base = _base(kpi, spec.of) if spec.of else None
        if base is not None and base.agg in NON_ADDITIVE:
            return _agg_detail(detail, kpi, base, group_dims or [], combo, target, target)
        return _point(series, kpi, spec.of, target)
    if spec.kind == "window":
        if kpi.time is None or plan is None:
            raise CatalogError(f"{spec.key} is a window measure; this KPI has no time column.")
        start, end = _window_bounds(plan.anchor, spec, kpi)
        base = _base(kpi, spec.of)
        if base.agg in NON_ADDITIVE:
            return _agg_detail(detail, kpi, base, group_dims or [], combo, start, end)
        return _window(series, kpi, spec, start, end)
    if spec.kind == "trend":
        if kpi.time is None or plan is None:
            raise CatalogError(f"{spec.key} is a trend measure; this KPI has no time column.")
        return _trend(series, kpi, spec, plan, detail=detail, combo=combo, group_dims=group_dims)
    if spec.kind in {"arithmetic", "fn", "expr"}:
        if spec.kind == "fn":
            names = list(spec.inputs)
        elif spec.kind == "expr":
            names = list(spec.inputs)
        else:
            names = list(spec.operands) or [spec.left or "", spec.right or ""]
        missing = [n for n in names if n not in catalog]
        if missing:
            raise CatalogError(f"{spec.key} references measures that do not exist: {missing}.")
        values = [
            evaluate(
                catalog[n],
                series,
                kpi,
                plan,
                catalog,
                detail,
                combo,
                group_dims,
                memo=memo,
            )
            for n in names
        ]
        if spec.kind == "expr":
            from kpi_engine.identifiers import parse_expression

            keys = list(spec.input_params) if spec.input_params else names
            return eval_expr_scalar(
                parse_expression(spec.expr or "", what=f"measures.{spec.key}.expr"),
                dict(zip(keys, values)),
            )
        if spec.kind == "fn":
            return call_measure_fn(spec.fn or "", values, spec.input_params)
        return call_measure_fn(spec.fn or "divide", values)
    if spec.kind == "constant":
        return spec.constant
    if spec.kind == "hook":
        from kpi_engine.extensions.hooks import run

        name = spec.hook or spec.fn
        if not name:
            raise CatalogError(f"measures.{spec.key} op=hook requires `hook:`.")
        return run(name, series, kpi=kpi, plan=plan, spec=spec)
    if spec.kind == "rank":
        raise CatalogError(f"{spec.key} op=rank is assigned after every combo on the cut.")
    raise CatalogError(f"Cannot evaluate {spec.key} kind={spec.kind}.")


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
    dims = list(cut_group_dims(cut, time_col or ""))
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


def _point(
    series: pd.DataFrame, kpi: KpiSpec, measure: str | None, target: date | None
) -> float | None:
    """Value at one calendar month, or the snapshot row when the KPI has no time column."""
    base = _base(kpi, measure)
    if kpi.time is None or target is None:
        if series.empty:
            return None
        row = series.iloc[0]
        return _value_from_row(row, base)
    ts = pd.Timestamp(target)
    hit = series[pd.to_datetime(series[kpi.time.column]) == ts]
    if hit.empty:
        return None
    return _value_from_row(hit.iloc[0], base)


def _value_from_row(row: pd.Series, base) -> float | None:
    """Read one aggregated base measure from a monthly/snapshot row."""
    if not bool(row.get("_observed", True)):
        return None
    if base.agg == "avg":
        total = row.get(f"{base.name}__sum")
        count = row.get(f"{base.name}__count")
        if pd.isna(total) or pd.isna(count) or count == 0:
            return None
        return _num(total / count)
    value = row[base.name]
    if pd.isna(value):
        return None
    return _num(value)


def _window(series: pd.DataFrame, kpi: KpiSpec, spec: OutputSpec, start: date, end: date) -> float | None:
    """Aggregate the base measure over [start, end] using the declared agg."""
    measure = _base(kpi, spec.of)
    ts = pd.to_datetime(series[kpi.time.column])
    window = series[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))]
    if window.empty:
        return None
    if measure.agg == "avg":
        total = window[f"{measure.name}__sum"].sum()
        count = window[f"{measure.name}__count"].sum()
        if count == 0:
            return None
        return _num(total / count)
    col = measure.name
    if measure.agg == "min":
        return _num_or_none(window[col].min())
    if measure.agg == "max":
        return _num_or_none(window[col].max())
    return _num_or_none(window[col].sum())


def _trend(
    series: pd.DataFrame,
    kpi: KpiSpec,
    spec: OutputSpec,
    plan: TimePlan | None,
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
) -> tuple[list[str], list[float | None]]:
    """Period series for a graph: fixed-length array aligned to a period axis."""
    start, end = _window_bounds(plan.anchor, spec, kpi)
    axis_dates = period_range_inclusive(start, end, kpi.time)
    axis = [iso_period(d, kpi.time) for d in axis_dates]
    measure = _base(kpi, spec.of)
    values: list[float | None] = []
    ts = pd.to_datetime(series[kpi.time.column]) if not series.empty and kpi.time.column in series.columns else None
    for month in axis_dates:
        if measure.agg in NON_ADDITIVE:
            values.append(_agg_detail(detail, kpi, measure, group_dims or [], combo, month, month))
            continue
        if ts is None:
            values.append(0.0 if measure.agg in {"sum", "count"} else None)
            continue
        hit = series[ts == pd.Timestamp(month)]
        if hit.empty:
            values.append(0.0 if measure.agg in {"sum", "count"} else None)
            continue
        row = hit.iloc[0]
        if not bool(row.get("_observed", True)):
            values.append(0.0 if measure.agg in {"sum", "count"} else None)
            continue
        if measure.agg == "avg":
            total = row.get(f"{measure.name}__sum")
            count = row.get(f"{measure.name}__count")
            if pd.isna(total) or pd.isna(count) or count == 0:
                values.append(None)
            else:
                values.append(_num(total / count))
            continue
        values.append(_num_or_none(row[measure.name]))
    return axis, values


def _window_bounds(anchor: date, spec: OutputSpec, kpi: KpiSpec) -> tuple[date, date]:
    """Inclusive trailing N through the anchor; leading N after; cumulative YTD."""
    from kpi_engine.dates import add_days, year_start

    kind = spec.window_range or "trailing"
    n = spec.trailing_months or 1
    if kind == "cumulative":
        return year_start(anchor, kpi.time), anchor
    if kind == "leading":
        if spec.inclusive:
            return anchor, add_periods(anchor, n - 1, kpi.time)
        return add_periods(anchor, 1, kpi.time), add_periods(anchor, n, kpi.time)
    if spec.trailing_unit == "day":
        if spec.inclusive:
            return add_days(anchor, -(n - 1)), anchor
        return add_days(anchor, -n), add_days(anchor, -1)
    if spec.inclusive:
        return add_periods(anchor, -(n - 1), kpi.time), anchor
    return add_periods(anchor, -n, kpi.time), add_periods(anchor, -1, kpi.time)


def truncate_period_safe(anchor: date, kpi: KpiSpec) -> date:
    """Truncate the anchor to the KPI grain."""
    from kpi_engine.dates import truncate_period

    if kpi.time is None:
        return anchor
    return truncate_period(anchor, kpi.time)


def _agg_detail(
    detail: pd.DataFrame | None,
    kpi: KpiSpec,
    base,
    group_dims: list[str],
    combo: pd.Series | None,
    start: date,
    end: date,
) -> float | None:
    """count_distinct / median / percentile from row-level rows in [start, end]."""
    if detail is None or detail.empty:
        return None
    work = detail
    if kpi.time is not None:
        ts = pd.to_datetime(work[kpi.time.column]).dt.normalize()
        work = work[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))]
    if combo is not None:
        for dim in group_dims:
            if dim in work.columns and dim in combo.index:
                work = work[work[dim] == combo[dim]]
    if work.empty:
        return None
    col = base.name if base.name in work.columns else (base.sql if base.sql in work.columns else None)
    if col is None or col not in work.columns:
        return None
    if base.agg in {"first", "last"}:
        order_col = "__event_time" if "__event_time" in work.columns else (
            kpi.time.column if kpi.time is not None and kpi.time.column in work.columns else None
        )
        if order_col:
            work = work.sort_values(order_col)
        series = work[col]
        value = series.iloc[0] if base.agg == "first" else series.iloc[-1]
        return _num_or_none(value)
    series = work[col]
    if base.agg == "count_distinct":
        return _num(series.nunique(dropna=True))
    if base.agg == "median":
        return _num_or_none(series.median())
    if base.agg == "percentile":
        q = base.percentile if base.percentile is not None else 0.5
        return _num_or_none(series.quantile(q))
    if base.agg == "first":
        return _num_or_none(series.iloc[0])
    if base.agg == "last":
        return _num_or_none(series.iloc[-1])
    return None


def _base(kpi: KpiSpec, name: str | None):
    """Look up a base_measures entry by name."""
    if not name:
        raise CatalogError("This op requires `of` to name a base measure.")
    for measure in kpi.base_measures:
        if measure.name == name:
            return measure
    raise CatalogError(f"Unknown base measure {name!r}.")


def _trend_applies(spec: OutputSpec, cut: CutSpec, kpi: KpiSpec) -> bool:
    """Whether this trend should appear on this cut (default: default_cut only)."""
    return _cut_limited_applies(spec, cut, kpi)


def _cut_limited_applies(spec: OutputSpec, cut: CutSpec, kpi: KpiSpec) -> bool:
    """Trend and rank default to default_cut unless measures.*.cuts lists more."""
    allowed = spec.cuts if spec.cuts is not None else (kpi.default_cut,)
    return cut.name in allowed


def _rank_source(
    spec: OutputSpec,
    series: pd.DataFrame,
    kpi: KpiSpec,
    plan: TimePlan | None,
    catalog: dict[str, OutputSpec],
    detail: pd.DataFrame | None,
    combo: pd.Series | None,
    group_dims: list[str],
    memo: dict[str, Any],
) -> Any:
    """Anchor value that this rank will order: another measure, or a base point."""
    of = spec.of
    if of and of in catalog:
        return evaluate(
            catalog[of],
            series,
            kpi,
            plan,
            catalog,
            detail=detail,
            combo=combo,
            group_dims=group_dims,
            memo=memo,
        )
    return evaluate(
        OutputSpec(key=of or spec.key, kind="point", of=of),
        series,
        kpi,
        plan,
        catalog,
        detail=detail,
        combo=combo,
        group_dims=group_dims,
        memo=memo,
    )


def _apply_ranks(
    cut_rows: list[dict[str, Any]],
    rank_keys: list[str],
    measures: dict[str, OutputSpec],
    cut_dims: list[str],
) -> None:
    """Write SQL RANK() values onto cut rows (ties share a rank; next rank skips)."""
    for key in rank_keys:
        spec = measures[key]
        src = f"__rank_src_{key}"
        descending = (spec.rank_order or "desc") == "desc"
        partition_keys = spec.rank_group_by
        if {norm_name(n) for n in partition_keys} == {norm_name(n) for n in cut_dims}:
            partition_keys = ()
        partitions: dict[tuple[Any, ...], list[int]] = {}
        for i, row in enumerate(cut_rows):
            part = _rank_partition(row, partition_keys)
            partitions.setdefault(part, []).append(i)
        for indexes in partitions.values():
            values = [cut_rows[i].get(src) for i in indexes]
            ranks = _sql_rank(values, descending=descending)
            for i, rank in zip(indexes, ranks):
                cut_rows[i][key] = rank
                cut_rows[i].pop(src, None)
                log_measure(
                    cut_rows[i].get("output_cut") or "",
                    key,
                    "rank",
                    {dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
                    rank,
                )


def _rank_partition(row: dict[str, Any], group_by: tuple[str, ...]) -> tuple[Any, ...]:
    """Partition key for rank; empty group_by is one list for the whole cut."""
    if not group_by:
        return ()
    by_norm = {norm_name(str(k)): v for k, v in row.items()}
    return tuple(by_norm.get(norm_name(name)) for name in group_by)


def _sql_rank(values: list[Any], *, descending: bool) -> list[int | None]:
    """RANK(): equal values share a rank; the next rank skips (1, 2, 2, 4)."""
    ranked: list[int | None] = [None] * len(values)
    order = [
        (i, v)
        for i, v in enumerate(values)
        if v is not None and not (isinstance(v, float) and pd.isna(v))
    ]
    order.sort(key=lambda item: item[1], reverse=descending)
    last_value: Any = object()
    rank = 0
    for pos, (i, value) in enumerate(order):
        if pos == 0 or value != last_value:
            rank = pos + 1
            last_value = value
        ranked[i] = rank
    return ranked


def _guard_trend_payload(
    row_count: int, trend_keys: list[str], catalog: dict[str, OutputSpec], cut: CutSpec
) -> None:
    """Fail if row_count × trend length would exceed TREND_CELL_CAP."""
    for key in trend_keys:
        length = catalog[key].trailing_months or 1
        cells = row_count * length
        if cells > TREND_CELL_CAP:
            raise KPIEngineError(
                f"Trend {key!r} on cut {cut.name!r} would emit {cells} cells "
                f"(cap {TREND_CELL_CAP}). Narrow the cut in measures.{key}.cuts."
            )


def _num(value: Any) -> float:
    """JSON-friendly float."""
    return float(value)


def _num_or_none(value: Any) -> float | None:
    """Float, or None when the aggregate is empty. NaN is not valid JSON."""
    if value is None or pd.isna(value):
        return None
    return float(value)


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
