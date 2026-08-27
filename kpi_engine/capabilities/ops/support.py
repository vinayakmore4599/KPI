"""Public helpers for measure op plugins. Do not import calc_engine from here."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.contracts import (
    FULL_RANGES,
    NAMED_WINDOW_RANGES,
    NON_ADDITIVE_AGGS,
    PTD_RANGES,
    CutSpec,
    KpiSpec,
    OutputSpec,
    TimePlan,
)
from kpi_engine.dates import add_periods, iso_period, period_range_inclusive
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.identifiers import norm_name, require_ident


def effective_anchor(ctx) -> date:
    """Override from a shifted evaluate, else the request plan.anchor."""
    if ctx.anchor is not None:
        return ctx.anchor
    if ctx.plan is not None and ctx.plan.anchor is not None:
        return ctx.plan.anchor
    raise CatalogError(f"{ctx.spec.key} needs a time plan (no effective anchor).")


def effective_selection(ctx) -> tuple[date, ...]:
    """Buckets this evaluation should fold. Explicit selection wins; else the plan.

    A per-period walk passes `ctx.selection` as a one-bucket tuple so a year
    selection does not leak into every trend point (filter-context replacement).
    """
    from kpi_engine.dates import truncate_period

    if getattr(ctx, "selection", None) is not None:
        return tuple(ctx.selection)
    if ctx.plan is not None and ctx.plan.selection and ctx.plan.selection.periods:
        return ctx.plan.selection.periods
    if ctx.anchor is not None:
        if ctx.kpi.time is not None:
            return (truncate_period(ctx.anchor, ctx.kpi.time),)
        return (ctx.anchor,)
    if ctx.plan is not None and ctx.plan.anchor is not None:
        return (ctx.plan.anchor,)
    return ()


def base_measure(kpi: KpiSpec, name: str | None):
    """Look up a base_measures entry by name."""
    if not name:
        raise CatalogError("This op requires `of` to name a base measure.")
    for measure in kpi.base_measures:
        if measure.name == name:
            return measure
    raise CatalogError(f"Unknown base measure {name!r}.")


def cut_limited_applies(spec: OutputSpec, cut: CutSpec, kpi: KpiSpec) -> bool:
    """Default to default_cut unless measures.*.cuts lists more."""
    allowed = spec.cuts if spec.cuts is not None else (kpi.default_cut,)
    return cut.name in allowed


def truncate_period_safe(anchor: date, kpi: KpiSpec) -> date:
    """Truncate the anchor to the KPI grain."""
    from kpi_engine.dates import truncate_period

    if kpi.time is None:
        return anchor
    return truncate_period(anchor, kpi.time)


def window_reference(anchor: date, spec: OutputSpec, kpi: KpiSpec) -> date:
    """Anchor shifted backward by `offset`, then truncated to the KPI grain."""
    return shifted_anchor(anchor, spec.offset, kpi, backward=True)


def window_bounds(anchor: date, spec: OutputSpec, kpi: KpiSpec) -> tuple[date, date]:
    """Named PTD/full period, or trailing/leading N, relative to the (offset) reference."""
    from kpi_engine.dates import (
        add_days,
        month_start,
        period_end,
        quarter_start,
        week_start,
        year_start,
    )

    ref = window_reference(anchor, spec, kpi)
    kind = spec.window_range or "trailing"
    if kind == "wtd" and (kpi.time is None or kpi.time.grain != "day"):
        raise BindError(f"measures.{spec.key} range=wtd needs time.grain: day.")
    if kind in {"ytd", "cumulative"}:
        return year_start(ref, kpi.time), ref
    if kind == "mtd":
        return month_start(ref), ref
    if kind == "qtd":
        return quarter_start(ref, kpi.time), ref
    if kind == "wtd":
        return week_start(ref), ref
    if kind in FULL_RANGES:
        return {
            "full_month": month_start(ref),
            "full_quarter": quarter_start(ref, kpi.time),
            "full_year": year_start(ref, kpi.time),
        }[kind], period_end(ref, kind, kpi.time)
    n = spec.trailing_months or 1
    if spec.params.get("align") == "periods":
        from dataclasses import replace as _replace

        spec = _replace(spec, trailing_unit=None)
    if kind == "leading":
        start, end = _count_window(ref, n, spec, kpi, leading=True)
        return start, end
    return _count_window(ref, n, spec, kpi, leading=False)


def _count_window(ref, n: int, spec: OutputSpec, kpi: KpiSpec, *, leading: bool):
    """Trailing/leading bounds. Calendar units stay calendar; periods follow the grain."""
    from kpi_engine.dates import add_days, add_periods, apply_offset, truncate_period
    from kpi_engine.contracts import Offset

    unit = spec.trailing_unit
    if leading:
        if unit in {None, kpi.time.grain if kpi.time else None}:
            if spec.inclusive:
                return ref, add_periods(ref, n - 1, kpi.time)
            return add_periods(ref, 1, kpi.time), add_periods(ref, n, kpi.time)
        start = ref if spec.inclusive else _calendar_shift(ref, 1, unit)
        end = _calendar_shift(ref, n - 1 if spec.inclusive else n, unit)
        return start, truncate_period(end, kpi.time) if kpi.time else end
    if unit in {None, kpi.time.grain if kpi.time else None}:
        if spec.inclusive:
            return add_periods(ref, -(n - 1), kpi.time), ref
        return add_periods(ref, -n, kpi.time), add_periods(ref, -1, kpi.time)
    if spec.inclusive:
        start = _calendar_shift(ref, -(n - 1), unit)
        return (truncate_period(start, kpi.time) if kpi.time else start), ref
    start = _calendar_shift(ref, -n, unit)
    end = _calendar_shift(ref, -1, unit)
    if kpi.time:
        return truncate_period(start, kpi.time), truncate_period(end, kpi.time)
    return start, end


def _calendar_shift(ref, steps: int, unit: str | None):
    """Shift `ref` by calendar days/weeks/months/quarters/years."""
    from kpi_engine.dates import add_days, apply_offset
    from kpi_engine.contracts import Offset

    if unit == "day":
        return add_days(ref, steps)
    if unit == "week":
        return add_days(ref, steps * 7)
    if unit == "month":
        return apply_offset(ref, Offset(months=steps))
    if unit == "quarter":
        return apply_offset(ref, Offset(quarters=steps))
    if unit == "year":
        return apply_offset(ref, Offset(years=steps))
    return apply_offset(ref, Offset(months=steps))


def window_lookback_periods(spec: OutputSpec, time, anchor) -> int:
    """Grain steps from window start to the request anchor (includes offset)."""
    from datetime import date as date_cls

    from kpi_engine.dates import periods_between, truncate_period

    kind = spec.window_range or "trailing"
    if time is None:
        if kind in NAMED_WINDOW_RANGES or kind == "leading":
            return 0
        n = spec.trailing_months or 1
        return max(n - 1, 0) if spec.inclusive else n
    dummy = KpiSpec(
        kpi_id=0,
        version=1,
        model_id="",
        time=time,
        dimensions=(),
        base_measures=(),
        cuts=(),
        default_cut="",
        measures=(),
    )
    ref_anchor = truncate_period(anchor or date_cls(2021, 12, 15), time)
    start, _end = window_bounds(ref_anchor, spec, dummy)
    if start >= ref_anchor:
        return 0
    return periods_between(start, ref_anchor, time)


def window_lookforward_periods(spec: OutputSpec, time, anchor) -> int:
    """Grain steps after the request anchor a full/leading window still needs."""
    from datetime import date as date_cls

    from kpi_engine.dates import periods_between, truncate_period

    if time is None:
        return 0
    dummy = KpiSpec(
        kpi_id=0,
        version=1,
        model_id="",
        time=time,
        dimensions=(),
        base_measures=(),
        cuts=(),
        default_cut="",
        measures=(),
    )
    ref_anchor = truncate_period(anchor or date_cls(2021, 6, 15), time)
    _start, end = window_bounds(ref_anchor, spec, dummy)
    if end <= ref_anchor:
        return 0
    return periods_between(ref_anchor, end, time)


def _plan_selection_periods(plan: TimePlan | None) -> tuple[date, ...]:
    """Request buckets: explicit selection, else the single anchor."""
    if plan is None:
        return ()
    if plan.selection is not None and plan.selection.periods:
        return tuple(plan.selection.periods)
    if plan.anchor is not None:
        return (plan.anchor,)
    return ()


def _offset_shifts(offset) -> bool:
    from kpi_engine.pipeline.op_protocol import offset_is_nonzero

    return offset_is_nonzero(offset)


def current_period_meta(kpi: KpiSpec, plan: TimePlan | None) -> dict[str, Any] | None:
    """``{period}`` for the request selection (unshifted)."""
    if kpi.time is None or plan is None:
        return None
    periods = _plan_selection_periods(plan)
    if not periods:
        return None
    return {"period": iso_period(periods[-1], kpi.time)}


def composite_period_meta(
    kpi: KpiSpec,
    plan: TimePlan | None,
    input_keys: tuple[str, ...],
) -> dict[str, Any] | None:
    """Period cell for fn / arithmetic / expr from input measure periods.

    When every timed input resolves to the same ``period``, use that bucket
    (e.g. a fn over a previous-month point). When inputs disagree, keep the
    request anchor (YoY / ratio at the selected month).
    """
    from kpi_engine.pipeline.op_registry import get_op

    by_key = {m.key: m for m in kpi.measures}
    periods_found: list[str] = []
    for key in input_keys:
        dep = by_key.get(key)
        if dep is None:
            continue
        meta = get_op(dep.kind).periods(dep, kpi, plan)
        if meta and meta.get("period"):
            periods_found.append(str(meta["period"]))
    if not periods_found:
        return current_period_meta(kpi, plan)
    unique = set(periods_found)
    if len(unique) == 1:
        return {"period": periods_found[0]}
    return current_period_meta(kpi, plan)


def compare_period_meta(
    spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None
) -> dict[str, Any] | None:
    """Request ``period`` plus ``baseline_period`` from a backward offset."""
    current = current_period_meta(kpi, plan)
    if not current:
        return None
    baseline = shift_period_meta(spec, kpi, plan, backward=True)
    if baseline and baseline.get("period"):
        return {**current, "baseline_period": baseline["period"]}
    return current


def shift_period_meta(
    spec: OutputSpec,
    kpi: KpiSpec,
    plan: TimePlan | None,
    *,
    backward: bool = True,
) -> dict[str, Any] | None:
    """``{period}`` for point / lag / lead after applying the measure offset."""
    if kpi.time is None or plan is None:
        return None
    from kpi_engine.pipeline.period_select import negate_offset, shift_selection

    periods = _plan_selection_periods(plan)
    if spec.offset and _offset_shifts(spec.offset):
        offset = negate_offset(spec.offset) if backward else spec.offset
        periods = shift_selection(periods, offset, kpi.time)
    if not periods:
        return None
    return {"period": iso_period(periods[-1], kpi.time)}


def window_period_meta(
    spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None
) -> dict[str, Any] | None:
    """``{period_start, period_end}`` for window / ytd / full_* / windowed hooks."""
    if kpi.time is None or plan is None:
        return None
    sel = _plan_selection_periods(plan)
    if not sel:
        return None
    start, end = window_bounds(sel[-1], spec, kpi)
    return {
        "period_start": iso_period(start, kpi.time),
        "period_end": iso_period(end, kpi.time),
    }


def trend_period_meta(
    spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None
) -> dict[str, Any] | None:
    """``{periods: [...]}`` aligned to ``trend_values`` (from the request anchor)."""
    if kpi.time is None or plan is None or plan.anchor is None:
        return None
    start, end = window_bounds(plan.anchor, spec, kpi)
    axis = [iso_period(d, kpi.time) for d in period_range_inclusive(start, end, kpi.time)]
    if not axis:
        return None
    return {"periods": axis}


def hook_period_meta(
    spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None
) -> dict[str, Any] | None:
    """Window metadata when the hook has a range/trailing; else a shifted point."""
    if spec.window_range or spec.trailing_months or spec.trailing_from:
        return window_period_meta(spec, kpi, plan)
    if _offset_shifts(spec.offset):
        return shift_period_meta(spec, kpi, plan, backward=True)
    return None


def assert_window_range(spec: OutputSpec, kpi: KpiSpec) -> None:
    """Reject illegal range + trailing / grain combinations."""
    kind = spec.window_range or "trailing"
    if kind in NAMED_WINDOW_RANGES and spec.trailing_months is not None:
        raise BindError(
            f"measures.{spec.key} range={kind} cannot set trailing:. "
            "Use trailing/leading for an N-period window."
        )
    if kind == "leading" and spec.trailing_months is None and not spec.trailing_from:
        raise BindError(f"measures.{spec.key} range=leading requires trailing: (the length).")
    if kind == "wtd":
        allowed = kpi.time.grains or ((kpi.time.grain,) if kpi.time else ())
        if kpi.time is None or "day" not in allowed:
            raise BindError(f"measures.{spec.key} range=wtd needs time.grain: day.")
        if not kpi.time.grains and kpi.time.grain != "day":
            raise BindError(f"measures.{spec.key} range=wtd needs time.grain: day.")


def point_value(
    series: pd.DataFrame, kpi: KpiSpec, measure: str | None, target: date | None
) -> float | None:
    """Value at one calendar month, or the snapshot row when there is no time column."""
    base = base_measure(kpi, measure)
    if kpi.time is None or target is None:
        if series.empty:
            return None
        return value_from_row(series.iloc[0], base)
    ts = pd.Timestamp(target)
    hit = series[pd.to_datetime(series[kpi.time.column]) == ts]
    if hit.empty:
        return None
    return value_from_row(hit.iloc[0], base)


def value_from_row(row: pd.Series, base) -> Any:
    """Read one aggregated base measure from a monthly/snapshot row."""
    if not bool(row.get("_observed", True)):
        return None
    if base.agg == "avg":
        total = row.get(f"{base.name}__sum")
        count = row.get(f"{base.name}__count")
        if pd.isna(total) or pd.isna(count) or count == 0:
            return None
        return float(total / count)
    if base.agg == "weighted_avg":
        total = row.get(f"{base.name}__wsum")
        weight = row.get(f"{base.name}__wcount")
        if pd.isna(total) or pd.isna(weight) or weight == 0:
            return None
        return float(total / weight)
    if base.name not in row.index:
        return None
    value = row[base.name]
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        import numpy as np

        if isinstance(value, np.datetime64):
            return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def selection_value(
    series: pd.DataFrame,
    kpi: KpiSpec,
    spec: OutputSpec,
    periods: tuple[date, ...],
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
) -> Any:
    """Fold the base measure over the selected buckets. None if none are observed."""
    if not periods:
        return None
    if len(periods) == 1:
        base = base_measure(kpi, spec.of)
        if base.agg in NON_ADDITIVE_AGGS:
            return agg_detail(
                detail, kpi, base, group_dims or [], combo, periods[0], periods[0]
            )
        return point_value(series, kpi, spec.of, periods[0])
    base = base_measure(kpi, spec.of)
    if base.agg in NON_ADDITIVE_AGGS:
        return agg_detail(
            detail,
            kpi,
            base,
            group_dims or [],
            combo,
            min(periods),
            max(periods),
            periods=periods,
        )
    if kpi.time is None or series.empty or kpi.time.column not in series.columns:
        return None
    ts = pd.to_datetime(series[kpi.time.column])
    wanted = {pd.Timestamp(p) for p in periods}
    window = series[ts.isin(wanted)]
    if "_observed" in window.columns:
        window = window[window["_observed"].astype(bool)]
    if window.empty:
        return None
    if base.agg == "avg":
        total = window[f"{base.name}__sum"].sum()
        count = window[f"{base.name}__count"].sum()
        if count == 0:
            return None
        return float(total / count)
    if base.agg == "weighted_avg":
        total = window[f"{base.name}__wsum"].sum(min_count=1)
        weight = window[f"{base.name}__wcount"].sum(min_count=1)
        if pd.isna(total) or pd.isna(weight) or weight == 0:
            return None
        return float(total / weight)
    col = base.name
    if col not in window.columns:
        return None
    if base.agg == "min":
        return _num_or_none(window[col].min())
    if base.agg == "max":
        return _num_or_none(window[col].max())
    return _num_or_none(window[col].sum())


def window_value(
    series: pd.DataFrame, kpi: KpiSpec, spec: OutputSpec, start: date, end: date
) -> float | None:
    """Aggregate the base measure over [start, end] using the declared agg."""
    measure = base_measure(kpi, spec.of)
    ts = pd.to_datetime(series[kpi.time.column])
    window = series[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))]
    if window.empty:
        return None
    if measure.agg == "avg":
        total = window[f"{measure.name}__sum"].sum()
        count = window[f"{measure.name}__count"].sum()
        if count == 0:
            return None
        return float(total / count)
    if measure.agg == "weighted_avg":
        total = window[f"{measure.name}__wsum"].sum(min_count=1)
        weight = window[f"{measure.name}__wcount"].sum(min_count=1)
        if pd.isna(total) or pd.isna(weight) or weight == 0:
            return None
        return float(total / weight)
    col = measure.name
    if measure.agg == "min":
        return _num_or_none(window[col].min())
    if measure.agg == "max":
        return _num_or_none(window[col].max())
    return _num_or_none(window[col].sum())


def operand_names(spec: OutputSpec) -> tuple[str, ...]:
    """`of: [a, b, …]` operands, else `left` / `right`."""
    if spec.operands:
        return spec.operands
    return tuple(n for n in (spec.left, spec.right) if n)


def effective_measure_cuts(spec: OutputSpec, kpi: KpiSpec) -> tuple[str, ...]:
    """cuts: list, or default_cut when omitted."""
    return spec.cuts if spec.cuts is not None else (kpi.default_cut,)


def window_fingerprint(spec: OutputSpec, kpi: KpiSpec) -> tuple[Any, ...]:
    """Fields two series-mode deps must share to zip."""
    return (
        spec.trailing_months,
        spec.trailing_from,
        spec.trailing_unit,
        spec.inclusive,
        spec.window_range,
        spec.offset,
        effective_measure_cuts(spec, kpi),
    )


def parent_declares_window(spec: OutputSpec) -> bool:
    """True when trailing, range, or a shifting offset is set on this spec."""
    if spec.trailing_months is not None or spec.trailing_from or spec.window_range:
        return True
    offset = spec.offset
    if offset is None:
        return False
    return bool(
        offset.months or offset.years or offset.days or offset.quarters or offset.weeks
    )


def trend_axis_dates(spec: OutputSpec, kpi: KpiSpec, plan: TimePlan | None) -> list[date]:
    """Grain steps for a base-mode trend / trend_arithmetic window."""
    if plan is None or plan.anchor is None or kpi.time is None:
        return []
    start, end = window_bounds(plan.anchor, spec, kpi)
    return list(period_range_inclusive(start, end, kpi.time))


def trend_slot_value(
    series: pd.DataFrame,
    kpi: KpiSpec,
    measure,
    month: date,
    ts: pd.Series | None,
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
) -> float | None:
    """One grain slot of a base, same fill as ``trend_values``."""
    if measure.agg in NON_ADDITIVE_AGGS:
        return agg_detail(detail, kpi, measure, group_dims or [], combo, month, month)
    zero_fill = measure.agg in {"sum", "count"}
    if ts is None:
        return 0.0 if zero_fill else None
    hit = series[ts == pd.Timestamp(month)]
    if hit.empty:
        return 0.0 if zero_fill else None
    row = hit.iloc[0]
    if not bool(row.get("_observed", True)):
        return 0.0 if zero_fill else None
    if measure.agg == "avg":
        total = row.get(f"{measure.name}__sum")
        count = row.get(f"{measure.name}__count")
        if pd.isna(total) or pd.isna(count) or count == 0:
            return None
        return float(total / count)
    if measure.agg == "weighted_avg":
        total = row.get(f"{measure.name}__wsum")
        weight = row.get(f"{measure.name}__wcount")
        if pd.isna(total) or pd.isna(weight) or weight == 0:
            return None
        return float(total / weight)
    return _num_or_none(row[measure.name])


def trend_values(
    series: pd.DataFrame,
    kpi: KpiSpec,
    spec: OutputSpec,
    plan: TimePlan | None,
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
) -> tuple[list[str], list[float | None] | None]:
    """Period series for a graph: fixed-length array aligned to a period axis."""
    if plan is None or plan.anchor is None:
        return [], None
    axis_dates = trend_axis_dates(spec, kpi, plan)
    axis = [iso_period(d, kpi.time) for d in axis_dates]
    measure = base_measure(kpi, spec.of)
    ts = (
        pd.to_datetime(series[kpi.time.column])
        if not series.empty and kpi.time is not None and kpi.time.column in series.columns
        else None
    )
    values = [
        trend_slot_value(
            series, kpi, measure, month, ts, detail, combo, group_dims
        )
        for month in axis_dates
    ]
    return axis, values


def trend_arithmetic_base_values(
    series: pd.DataFrame,
    kpi: KpiSpec,
    spec: OutputSpec,
    plan: TimePlan | None,
    names: tuple[str, ...],
    detail: pd.DataFrame | None = None,
    combo: pd.Series | None = None,
    group_dims: list[str] | None = None,
) -> tuple[list[str], list[list[float | None]]]:
    """Per-operand slot lists aligned to this spec's trend axis."""
    if plan is None or plan.anchor is None:
        return [], [[] for _ in names]
    axis_dates = trend_axis_dates(spec, kpi, plan)
    axis = [iso_period(d, kpi.time) for d in axis_dates]
    ts = (
        pd.to_datetime(series[kpi.time.column])
        if not series.empty and kpi.time is not None and kpi.time.column in series.columns
        else None
    )
    columns: list[list[float | None]] = []
    for name in names:
        measure = base_measure(kpi, name)
        columns.append(
            [
                trend_slot_value(
                    series, kpi, measure, month, ts, detail, combo, group_dims
                )
                for month in axis_dates
            ]
        )
    return axis, columns


LIST_AGG_MAX_ITEMS = 1000
STRING_AGG_MAX_BYTES = 65536


def agg_detail(
    detail: pd.DataFrame | None,
    kpi: KpiSpec,
    base,
    group_dims: list[str],
    combo: pd.Series | None,
    start: date,
    end: date,
    periods: tuple[date, ...] | None = None,
) -> Any:
    """Non-additive aggs from row-level rows in [start, end]. Empty window is null."""
    if detail is None or detail.empty:
        return None
    work = detail
    if kpi.time is not None:
        ts = pd.to_datetime(work[kpi.time.column]).dt.normalize()
        if periods is not None:
            wanted = {pd.Timestamp(p) for p in periods}
            work = work[ts.isin(wanted)]
        else:
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
        order_col = (
            "__event_time"
            if "__event_time" in work.columns
            else (
                kpi.time.column
                if kpi.time is not None and kpi.time.column in work.columns
                else None
            )
        )
        if order_col:
            work = work.sort_values(order_col)
        series = work[col]
        value = series.iloc[0] if base.agg == "first" else series.iloc[-1]
        if isinstance(value, (list, tuple)):
            return list(value)
        return _num_or_none(value)
    series = work[col]
    if base.agg == "count_distinct":
        return float(series.nunique(dropna=True))
    if base.agg == "median":
        return _num_or_none(series.median())
    if base.agg == "percentile":
        q = base.percentile if base.percentile is not None else 0.5
        return _num_or_none(series.quantile(q))
    if base.agg == "stddev":
        if len(series.dropna()) < 2:
            return None
        return _num_or_none(series.std(ddof=1))
    if base.agg == "variance":
        if len(series.dropna()) < 2:
            return None
        return _num_or_none(series.var(ddof=1))
    if base.agg == "mode":
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return None
        modes = numeric.mode()
        if modes.empty:
            return None
        return _num_or_none(modes.iloc[0])
    if base.agg == "geomean":
        numeric = pd.to_numeric(series, errors="coerce")
        pos = numeric[numeric > 0]
        if pos.empty:
            return None
        import math

        return _num_or_none(math.exp(float(pos.map(math.log).mean())))
    if base.agg == "harmonic_mean":
        numeric = pd.to_numeric(series, errors="coerce")
        pos = numeric[numeric > 0]
        if pos.empty:
            return None
        return _num_or_none(len(pos) / (1.0 / pos).sum())
    if base.agg == "any":
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return None
        return 1.0 if bool((numeric != 0).any()) else 0.0
    if base.agg == "all":
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return None
        return 1.0 if bool((numeric != 0).all()) else 0.0
    if base.agg in {"list_agg", "string_agg"}:
        return _list_or_string_agg(series, base.agg)
    return None


def _list_or_string_agg(series: pd.Series, agg: str) -> Any:
    """Ordered non-null values; list_agg cap 1000, string_agg cap 64KB."""
    items: list[Any] = []
    for value in series.tolist():
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        if isinstance(value, pd.Timestamp):
            items.append(value.date().isoformat())
        elif hasattr(value, "item") and not isinstance(value, (bytes, str)):
            try:
                items.append(value.item())
            except (ValueError, AttributeError):
                items.append(value)
        else:
            items.append(value)
        if len(items) > LIST_AGG_MAX_ITEMS:
            raise CatalogError(
                f"{agg} overflow: more than {LIST_AGG_MAX_ITEMS} items in one group."
            )
    if not items:
        return None
    if agg == "list_agg":
        return items
    text = ",".join("" if part is None else str(part) for part in items)
    if len(text.encode("utf-8")) > STRING_AGG_MAX_BYTES:
        raise CatalogError(
            f"string_agg overflow: joined length exceeds {STRING_AGG_MAX_BYTES} bytes."
        )
    return text


def dense_rank(values: list[Any], *, descending: bool) -> list[int | None]:
    """DENSE_RANK(): equal values share a rank; the next rank does not skip."""
    ranked: list[int | None] = [None] * len(values)
    order = [
        (i, v)
        for i, v in enumerate(values)
        if v is not None and not (isinstance(v, float) and pd.isna(v))
    ]
    order.sort(key=lambda item: item[1], reverse=descending)
    last_value: Any = object()
    rank = 0
    for i, value in order:
        if value != last_value:
            rank += 1
            last_value = value
        ranked[i] = rank
    return ranked


def row_numbers(
    values: list[Any],
    *,
    descending: bool,
    tie_keys: list[tuple[Any, ...]],
) -> list[int]:
    """Unique 1..n. Nulls sort last; remaining ties use `tie_keys`."""
    indexed = list(enumerate(values))

    def sort_key(item: tuple[int, Any]) -> tuple:
        i, value = item
        number = numeric_or_none(value)
        null = number is None
        ordered = 0.0 if null else (-number if descending else number)
        return (null, ordered, tie_keys[i])

    order = sorted(indexed, key=sort_key)
    out = [0] * len(values)
    for pos, (i, _) in enumerate(order, start=1):
        out[i] = pos
    return out


def ntile_from_ranks(ranks: list[int | None], tiles: int) -> list[int | None]:
    """Map RANK() onto 1..tiles via ceil(rank * tiles / n)."""
    import math

    n = sum(1 for rank in ranks if rank is not None)
    if n == 0:
        return [None] * len(ranks)
    out: list[int | None] = []
    for rank in ranks:
        if rank is None:
            out.append(None)
        else:
            out.append(int(math.ceil(rank * tiles / n)))
    return out


def negate_offset(offset):
    """Flip a calendar offset (point/lag treat YAML offset as backwards)."""
    from kpi_engine.pipeline.period_select import negate_offset as _negate

    return _negate(offset)


def shifted_anchor(anchor: date, offset, kpi: KpiSpec, *, backward: bool) -> date:
    """Anchor plus or minus `offset`, truncated to the KPI grain."""
    from kpi_engine.pipeline.period_select import apply_measure_offset

    if offset is None:
        return truncate_period_safe(anchor, kpi)
    applied = negate_offset(offset) if backward else offset
    if kpi.time is None:
        from kpi_engine.dates import apply_offset

        return apply_offset(anchor, applied)
    return apply_measure_offset(anchor, applied, kpi.time)


def offset_lookback(offset, time, anchor) -> int:
    """Grain periods an offset reaches behind the anchor."""
    if offset is None:
        return 0
    if offset.periods:
        return abs(offset.periods)
    if time is None or (
        time.grain == "month"
        and offset.days == 0
        and offset.quarters == 0
        and offset.weeks == 0
        and offset.periods == 0
    ):
        return offset.total_months
    from kpi_engine.dates import periods_between, truncate_period
    from kpi_engine.pipeline.period_select import apply_measure_offset
    from datetime import date as date_cls

    dummy = date_cls(2021, 6, 15)
    dummy_t = truncate_period(dummy, time)
    target = apply_measure_offset(dummy, negate_offset(offset), time)
    return periods_between(target, dummy_t, time)


def sql_rank(
    values: list[Any],
    *,
    descending: bool,
    tie_keys: list[tuple[Any, ...]] | None = None,
) -> list[int | None]:
    """RANK(): equal values share a rank; the next rank skips (1, 2, 2, 4)."""
    ranked: list[int | None] = [None] * len(values)
    ties = tie_keys or [() for _ in values]
    order = [
        (i, v)
        for i, v in enumerate(values)
        if v is not None and not (isinstance(v, float) and pd.isna(v))
    ]

    def sort_key(item: tuple[int, Any]) -> tuple:
        i, value = item
        number = numeric_or_none(value)
        null = number is None
        ordered = 0.0 if null else (-number if descending else number)
        return (null, ordered, ties[i])

    order.sort(key=sort_key)
    last_key: Any = object()
    rank = 0
    for pos, (i, value) in enumerate(order):
        key = (value, ties[i])
        if pos == 0 or key != last_key:
            rank = pos + 1
            last_key = key
        ranked[i] = rank
    return ranked


def rank_partition(row: dict[str, Any], group_by: tuple[str, ...]) -> tuple[Any, ...]:
    """Partition key for rank; empty group_by is one list for the whole cut."""
    if not group_by:
        return ()
    by_norm = {norm_name(str(k)): v for k, v in row.items()}
    return tuple(by_norm.get(norm_name(name)) for name in group_by)


def log_base_calc(
    spec: OutputSpec,
    cut: str,
    combo: dict[str, Any],
    result: Any,
    series: pd.DataFrame,
    kpi: KpiSpec,
    *,
    start: date | None,
    end: date | None,
) -> None:
    """Log a point/window: which base column, which periods, which values."""
    from kpi_engine.runlog import log_measure_calc

    base = None
    if spec.of:
        try:
            base = base_measure(kpi, spec.of)
        except CatalogError:
            base = None
    used = used_points(series, kpi, base, start, end) if base is not None else []
    log_measure_calc(
        cut=cut,
        key=spec.key,
        op=spec.kind,
        combo=combo,
        result=result,
        of=spec.of,
        column=base.name if base is not None else None,
        source=base.sql if base is not None else None,
        agg=base.agg if base is not None else None,
        period=start if start is not None and start == end else None,
        window_start=start if start is not None and start != end else None,
        window_end=end if start is not None and start != end else None,
        used=used,
    )


def used_points(
    series: pd.DataFrame,
    kpi: KpiSpec,
    base,
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    """Period/value pairs read from the monthly series for this calculation."""
    if series is None or series.empty or base is None:
        return []
    work = series
    if (
        kpi.time is not None
        and kpi.time.column in work.columns
        and start is not None
        and end is not None
    ):
        ts = pd.to_datetime(work[kpi.time.column])
        work = work[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))]
    out: list[dict[str, Any]] = []
    time_col = kpi.time.column if kpi.time is not None else None
    has_col = (
        base.name in work.columns
        or f"{base.name}__sum" in work.columns
        or f"{base.name}__wsum" in work.columns
    )
    if not has_col:
        return []
    for _, row in work.iterrows():
        period = None
        if time_col and time_col in row.index and not pd.isna(row[time_col]):
            period = iso_period(pd.to_datetime(row[time_col]).date(), kpi.time)
        out.append(
            {
                "period": period,
                "column": base.name,
                "value": value_from_row(row, base),
                "observed": bool(row.get("_observed", True)),
            }
        )
    return out


def sample_mean_var(values: list[float]) -> tuple[float, float] | None:
    """Sample mean and variance. Needs at least two values."""
    if len(values) < 2:
        return None
    mean = sum(values) / float(len(values))
    var = sum((v - mean) ** 2 for v in values) / float(len(values) - 1)
    return mean, var


def numeric_or_none(value: Any) -> float | None:
    """Coerce a stashed source to float; null/NaN stay None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _num_or_none(value: Any) -> float | None:
    """Float, or None when the aggregate is empty."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def parse_fn_inputs(key: str, raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read `inputs:` as an ordered list or a {parameter: measure} mapping."""
    if isinstance(raw, dict):
        return tuple(str(v) for v in raw.values()), tuple(str(k) for k in raw)
    if not isinstance(raw, (list, tuple)):
        raise BindError(
            f"measures.{key}.inputs must be a list of measure keys or a "
            "{parameter: measure} mapping."
        )
    return tuple(str(x) for x in raw), ()


def parse_partition_by(key: str, kind: str, raw: dict[str, Any]) -> tuple[str, ...]:
    """Read partition_by: (preferred) or group_by: as dimension names."""
    if raw.get("partition_by") is not None:
        raw_group = raw.get("partition_by")
        what = "partition_by"
    else:
        raw_group = raw.get("group_by") or []
        what = "group_by"
    if not isinstance(raw_group, (list, tuple)):
        raise BindError(f"measures.{key} op={kind} {what} must be a list.")
    return tuple(require_ident(str(c), what=f"{kind} {what}") for c in raw_group)


def require_base_of(spec: OutputSpec, kpi: KpiSpec) -> None:
    """`of` must name a declared base measure. Row helpers are gated later."""
    known = {b.name: b for b in kpi.base_measures}
    if not spec.of:
        raise BindError(
            f"measures.{spec.key} op={spec.kind} requires `of:` naming the base "
            f"measure it aggregates. Declared base_measures: {sorted(known)}."
        )
    if spec.of not in known:
        raise BindError(
            f"measures.{spec.key} of={spec.of!r} is not a base measure. "
            f"Declared base_measures: {sorted(known)}."
        )


def reject_helper_of(spec: OutputSpec, kpi: KpiSpec) -> None:
    """Window / calendar shift cannot take a row helper as `of`."""
    known = {b.name: b for b in kpi.base_measures}
    base = known.get(spec.of or "")
    if base is not None and base.agg is None:
        raise BindError(
            f"measures.{spec.key} of={spec.of!r} is a row helper (no agg:). "
            "Add agg: on that base, or use it only in later base expr/over."
        )


def helper_names_used_as_of(kpi: KpiSpec, keys: tuple[str, ...]) -> tuple[str, ...]:
    """Row helpers named by requested measures as `of` / inputs."""
    bases = {b.name: b for b in kpi.base_measures}
    measures = {m.key: m for m in kpi.measures}
    found: list[str] = []
    seen: set[str] = set()

    def walk(name: str) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        base = bases.get(name)
        if base is not None and base.agg is None:
            found.append(base.name)
        spec = measures.get(name)
        if spec is None:
            return
        for dep in (spec.of, spec.left, spec.right, *spec.operands, *spec.inputs):
            if dep:
                walk(dep)

    for key in keys:
        walk(key)
    return tuple(dict.fromkeys(found))


def assert_helper_of_allowed(
    kpi: KpiSpec, emitted: tuple, keys: tuple[str, ...]
) -> None:
    """Point `of` a helper only when every emitted cut equals identity_grain."""
    helpers = helper_names_used_as_of(kpi, keys)
    if not helpers:
        return
    first = helpers[0]
    if not kpi.identity_grain:
        raise BindError(
            f"of={first!r} is a row helper (no agg:). "
            "Add agg: on that base, declare identity_grain: and emit only that "
            "grain, or use it only in later base expr/over."
        )
    from kpi_engine.pipeline.cuts import effective_group_by
    from kpi_engine.identifiers import norm_name

    wanted = {norm_name(name) for name in kpi.identity_grain}
    for cut in emitted:
        grain = effective_group_by(cut, kpi)
        have = {norm_name(name) for name in grain}
        if have != wanted:
            raise BindError(
                f"Row helper {first!r} can be measures.of only when every emitted "
                f"cut's grain equals identity_grain {list(kpi.identity_grain)}. "
                f"cuts.{cut.name} grain is {list(grain)}."
            )


def is_last_n_valued(name: str, kpi: KpiSpec, *, _seen: set[str] | None = None) -> bool:
    """True when `name` is a last_n base or a point that folds one."""
    seen = _seen if _seen is not None else set()
    if not name or name in seen:
        return False
    seen.add(name)
    bases = {b.name: b for b in kpi.base_measures}
    base = bases.get(name)
    if base is not None and base.over is not None and base.over.fn == "last_n":
        return True
    measures = {m.key: m for m in kpi.measures}
    spec = measures.get(name)
    if spec is None:
        return False
    if spec.kind == "point" and spec.of:
        return is_last_n_valued(spec.of, kpi, _seen=seen)
    for dep in (spec.of, spec.left, spec.right, *spec.operands, *spec.inputs):
        if dep and is_last_n_valued(dep, kpi, _seen=seen):
            return True
    return False


def assert_last_n_consumers(kpi: KpiSpec) -> None:
    """last_n JSON lists may sit on `op: point`; numeric consumers BindError."""
    skip = {"point", "dimension", "constant"}
    for spec in kpi.measures:
        if spec.kind in skip:
            continue
        names = [
            n
            for n in (spec.of, spec.left, spec.right, *spec.operands, *spec.inputs)
            if n
        ]
        vs = spec.params.get("vs") if spec.params else None
        if vs:
            names.append(str(vs))
        for name in names:
            if is_last_n_valued(name, kpi):
                raise BindError(
                    f"measures.{spec.key} cannot use {name!r}: last_n is a JSON "
                    "list, not a numeric measure. Keep it on op: point."
                )
    if kpi.green_when is not None and is_last_n_valued(kpi.green_when.of, kpi):
        raise BindError(
            f"green_when.of={kpi.green_when.of!r} is last_n (JSON list); "
            "green_when needs a numeric measure."
        )
    if kpi.having is not None:
        from kpi_engine.pipeline.predicates import predicate_names

        for name in predicate_names(kpi.having.predicates):
            if is_last_n_valued(name, kpi):
                raise BindError(
                    f"having of={name!r} is last_n (JSON list); having needs a "
                    "numeric measure."
                )


def require_measure_or_base_of(spec: OutputSpec, kpi: KpiSpec) -> None:
    """`of` must name a measure or a base measure."""
    by_key = {m.key for m in kpi.measures}
    known = {b.name for b in kpi.base_measures}
    if not spec.of:
        raise BindError(
            f"measures.{spec.key} op={spec.kind} requires `of:` naming a measure "
            f"or base_measure. Declared measures: {sorted(by_key)}; "
            f"base_measures: {sorted(known)}."
        )
    if spec.of not in by_key and spec.of not in known:
        raise BindError(
            f"measures.{spec.key} of={spec.of!r} is not a measure or base "
            f"measure. Declared measures: {sorted(by_key)}; "
            f"base_measures: {sorted(known)}."
        )
    by_base = {b.name: b for b in kpi.base_measures}
    helper = by_base.get(spec.of)
    if helper is not None and helper.agg is None:
        raise BindError(
            f"measures.{spec.key} of={spec.of!r} is a row helper (no agg:). "
            "Add agg: on that base, or use it only in later base expr/over."
        )


def monthly_fact_columns(
    kpi: KpiSpec, measure_keys: tuple[str, ...] | None = None
) -> list[str]:
    """Densify value columns: folded facts plus helpers used as `of`."""
    keys = measure_keys if measure_keys is not None else tuple(m.key for m in kpi.measures)
    needed = set(helper_names_used_as_of(kpi, keys))
    cols: list[str] = []
    for measure in kpi.base_measures:
        if measure.name in needed:
            cols.append(measure.name)
            continue
        if measure.agg is None or measure.agg in NON_ADDITIVE_AGGS:
            continue
        if measure.agg == "avg":
            cols.extend([f"{measure.name}__sum", f"{measure.name}__count"])
            continue
        if measure.agg == "weighted_avg":
            cols.extend([f"{measure.name}__wsum", f"{measure.name}__wcount"])
            continue
        cols.append(measure.name)
    return cols


def assert_partition_keys(spec: OutputSpec, kpi: KpiSpec) -> None:
    """partition_by names must be catalog dimension names."""
    allowed = {norm_name(name): name for name in kpi.dimensions}
    valid = sorted(allowed.values())
    for name in spec.rank_group_by:
        if norm_name(name) in allowed:
            continue
        raise BindError(
            f"measures.{spec.key} partition_by {name!r} is not a dimension. "
            f"Valid: {valid}."
        )


def assert_partition_keys_for_request(kpi: KpiSpec, measure_keys: tuple[str, ...]) -> None:
    """Requested rank/share partition_by must sit on each cut the measure emits."""
    from kpi_engine.pipeline.cuts import effective_group_by
    from kpi_engine.pipeline.pipelines import cuts_for_keys

    by_key = {m.key: m for m in kpi.measures}
    for key in measure_keys:
        spec = by_key.get(key)
        if spec is None or not spec.rank_group_by:
            continue
        for cut in cuts_for_keys(kpi, (key,)):
            grain = effective_group_by(cut, kpi)
            allowed = {norm_name(name) for name in grain}
            for name in spec.rank_group_by:
                if norm_name(name) in allowed:
                    continue
                raise BindError(
                    f"measures.{spec.key} partition_by {name!r} is not in cut "
                    f"{cut.name!r} effective group_by {list(grain)}."
                )
