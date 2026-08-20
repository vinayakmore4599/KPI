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
    - Arithmetic: growth_pct / div; /0 and /null → null.
    - Trends default to default_cut only unless measures.*.cuts lists more.

When to use
    Add a new op kind here (and in binder._parse_measure). KPI YAML should
    reuse existing ops via `of` / `op` rather than custom Python.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.contracts import (
    BoundFilter,
    CutSpec,
    KpiSpec,
    OutputSpec,
    TimePlan,
)
from kpi_engine.core.cuts import cut_group_dims
from kpi_engine.core.filters import apply_cut_filters
from kpi_engine.dates import add_months, iso_month, month_range_inclusive
from kpi_engine.exceptions import CatalogError, KPIEngineError

TREND_CELL_CAP = 50_000


def densify(
    frame: pd.DataFrame,
    *,
    keys: list[str],
    time_col: str,
    start: date,
    end: date,
    value_cols: list[str],
    fill_zero_cols: list[str],
) -> pd.DataFrame:
    """Fill every partition with every month in [start, end] so shifts move by calendar."""
    work = frame.copy()
    work[time_col] = pd.to_datetime(work[time_col]).dt.to_period("M").dt.to_timestamp()
    months = pd.to_datetime(month_range_inclusive(start, end))
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
            merged[col] = merged[col].fillna(0)
    return merged


def compute_cuts(
    monthly: pd.DataFrame,
    *,
    kpi: KpiSpec,
    emitted: tuple[CutSpec, ...],
    deferred_filters: tuple[BoundFilter, ...],
    plan: TimePlan,
    requested: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Evaluate requested measures at each cut; return JSON rows and shared trend axes."""
    measures = {m.key: m for m in kpi.measures}
    need = [k for k in requested if measures[k].kind != "dimension"]
    dim_keys = [k for k in requested if measures[k].kind == "dimension"]
    rows: list[dict[str, Any]] = []
    trend_axes: dict[str, list[str]] = {}

    for cut in emitted:
        cut_monthly = _cut_monthly(monthly, cut, deferred_filters, kpi)
        group_dims = list(cut_group_dims(cut, kpi.time.column))
        if cut_monthly.empty and not group_dims:
            combo_frame = pd.DataFrame([{}])
        elif cut_monthly.empty:
            continue
        else:
            combo_frame = cut_monthly[group_dims].drop_duplicates() if group_dims else pd.DataFrame([{}])

        trend_keys = [
            k
            for k in need
            if measures[k].kind == "trend" and _trend_applies(measures[k], cut, kpi)
        ]
        _guard_trend_payload(len(combo_frame), trend_keys, measures, cut)

        for _, combo in combo_frame.iterrows():
            series = _combo_series(cut_monthly, group_dims, combo, kpi.time.column)
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
                if spec.kind == "trend" and not _trend_applies(spec, cut, kpi):
                    continue
                value = evaluate(spec, series, kpi, plan, measures)
                if spec.kind == "trend":
                    axis, values = value
                    trend_axes[key] = axis
                    row[key] = values
                else:
                    row[key] = value
            rows.append(row)
    return rows, trend_axes


def evaluate(
    spec: OutputSpec,
    series: pd.DataFrame,
    kpi: KpiSpec,
    plan: TimePlan,
    catalog: dict[str, OutputSpec],
) -> Any:
    """Dispatch one measure op against a single partition's monthly series."""
    if spec.kind == "point":
        offset = spec.offset.total_months if spec.offset else 0
        target = add_months(plan.anchor, -offset)
        return _point(series, kpi.time.column, spec.of, target)
    if spec.kind == "window":
        start, end = _window_bounds(plan.anchor, spec)
        return _window(series, kpi, spec, start, end)
    if spec.kind == "trend":
        return _trend(series, kpi, spec, plan)
    if spec.kind == "arithmetic":
        left = evaluate(catalog[spec.left or ""], series, kpi, plan, catalog)
        right = evaluate(catalog[spec.right or ""], series, kpi, plan, catalog)
        return _arithmetic(spec.fn or "div", left, right)
    raise CatalogError(f"Cannot evaluate {spec.key} kind={spec.kind}.")


def _cut_monthly(
    monthly: pd.DataFrame,
    cut: CutSpec,
    deferred: tuple[BoundFilter, ...],
    kpi: KpiSpec,
) -> pd.DataFrame:
    """Filter then re-aggregate the monthly frame to this cut's group_by."""
    work = apply_cut_filters(monthly, cut, deferred)
    time_col = kpi.time.column
    dims = list(cut_group_dims(cut, time_col))
    value_cols = [m.name for m in kpi.base_measures]
    extra = [f"{m.name}__sum" for m in kpi.base_measures if m.agg == "avg"]
    extra += [f"{m.name}__count" for m in kpi.base_measures if m.agg == "avg"]
    cols = [c for c in [*value_cols, *extra, "_observed"] if c in work.columns]
    group = [*dims, time_col]
    if work.empty:
        return work
    agg: dict[str, str] = {}
    for col in cols:
        agg[col] = "sum" if col != "_observed" else "max"
    out = work.groupby(group, dropna=False, as_index=False).agg(agg)
    out["_observed"] = out["_observed"].astype(bool)
    return out


def _combo_series(
    cut_monthly: pd.DataFrame,
    group_dims: list[str],
    combo: pd.Series,
    time_col: str,
) -> pd.DataFrame:
    """Rows for one dimension combination, ordered by time."""
    work = cut_monthly
    for dim in group_dims:
        work = work[work[dim] == combo[dim]]
    return work.sort_values(time_col)


def _point(series: pd.DataFrame, time_col: str, measure: str | None, target: date) -> float | None:
    """Value at one calendar month. Missing/unobserved months return null, not a shifted row."""
    if not measure:
        raise CatalogError("point op requires `of`.")
    ts = pd.Timestamp(target)
    hit = series[pd.to_datetime(series[time_col]) == ts]
    if hit.empty:
        return None
    row = hit.iloc[0]
    if not bool(row.get("_observed", True)):
        return None
    value = row[measure]
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
        return _num(window[col].min())
    if measure.agg == "max":
        return _num(window[col].max())
    return _num(window[col].sum())


def _trend(
    series: pd.DataFrame, kpi: KpiSpec, spec: OutputSpec, plan: TimePlan
) -> tuple[list[str], list[float | None]]:
    """Monthly series for a graph: fixed-length array aligned to a period axis."""
    start, end = _window_bounds(plan.anchor, spec)
    axis_dates = month_range_inclusive(start, end)
    axis = [iso_month(d) for d in axis_dates]
    measure = _base(kpi, spec.of)
    values: list[float | None] = []
    ts = pd.to_datetime(series[kpi.time.column])
    for month in axis_dates:
        hit = series[ts == pd.Timestamp(month)]
        if hit.empty:
            values.append(0.0 if measure.agg in {"sum", "count"} else None)
            continue
        row = hit.iloc[0]
        if not bool(row.get("_observed", True)):
            values.append(0.0 if measure.agg in {"sum", "count"} else None)
            continue
        values.append(_num(row[measure.name]))
    return axis, values


def _window_bounds(anchor: date, spec: OutputSpec) -> tuple[date, date]:
    """Inclusive: last N months through the anchor. Exclusive: N months before the anchor."""
    n = spec.trailing_months or 1
    if spec.inclusive:
        return add_months(anchor, -(n - 1)), anchor
    return add_months(anchor, -n), add_months(anchor, -1)


def _arithmetic(fn: str, left: Any, right: Any) -> float | None:
    """Combine two scalars. Division by zero or null yields null, never inf."""
    if fn in {"growth_pct", "yoy", "mom"}:
        if left is None or right in (None, 0):
            return None
        return _num((left - right) / right)
    if fn in {"div", "percent"}:
        if left is None or right in (None, 0):
            return None
        value = left / right
        return _num(value * 100) if fn == "percent" else _num(value)
    if left is None or right is None:
        return None
    if fn == "add":
        return _num(left + right)
    if fn == "sub":
        return _num(left - right)
    if fn == "mul":
        return _num(left * right)
    raise CatalogError(f"Unknown arithmetic fn {fn!r}.")


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
    allowed = spec.cuts if spec.cuts is not None else (kpi.default_cut,)
    return cut.name in allowed


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
