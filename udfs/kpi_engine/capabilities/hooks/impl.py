"""Named series hooks. Register via registries/hooks.yaml."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.capabilities.ops import support
from kpi_engine.dates import period_range_inclusive
from kpi_engine.exceptions import CatalogError


def _at(series: pd.DataFrame, time_col: str, measure: str, month: date) -> float | None:
    """Read one densified month from the hook's series argument."""
    if series is None or series.empty or time_col not in series.columns:
        return None
    hit = series[pd.to_datetime(series[time_col]) == pd.Timestamp(month)]
    if hit.empty:
        return None
    row = hit.iloc[0]
    if not bool(row.get("_observed", True)):
        return None
    if measure not in row.index:
        return None
    value = row[measure]
    if pd.isna(value):
        return None
    return float(value)


def _window_dates(kpi, plan, spec) -> list[date]:
    if kpi.time is None or plan is None:
        return []
    start, end = support.window_bounds(plan.anchor, spec, kpi)
    return period_range_inclusive(start, end, kpi.time)


def _observed_pairs(series, kpi, plan, spec) -> list[tuple[date, float]]:
    if kpi.time is None or plan is None or not spec.of:
        return []
    out: list[tuple[date, float]] = []
    for month in _window_dates(kpi, plan, spec):
        value = _at(series, kpi.time.column, spec.of, month)
        if value is not None:
            out.append((month, value))
    return out


def seasonal_index(series, *, kpi, plan, spec, **_):
    """Anchor / mean of the same calendar month in prior years inside the window."""
    if kpi.time is None or plan is None or not spec.of:
        return None
    anchor = support.truncate_period_safe(plan.anchor, kpi)
    current = _at(series, kpi.time.column, spec.of, anchor)
    if current is None:
        return None
    priors = [
        value
        for month, value in _observed_pairs(series, kpi, plan, spec)
        if month < anchor and month.month == anchor.month
    ]
    if not priors:
        return None
    return float(current) / (sum(priors) / len(priors))


def ewma(series, *, kpi, plan, spec, **_):
    """Exponentially weighted average at the anchor. Alpha = 2 / (N + 1)."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if not pairs:
        return None
    n = spec.trailing_months or len(pairs)
    alpha = 2.0 / (n + 1)
    value = pairs[0][1]
    for _, item in pairs[1:]:
        value = alpha * item + (1 - alpha) * value
    return float(value)


def period_max(series, *, kpi, plan, spec, **_):
    """Largest observed period value in the trailing window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    return None if not values else float(max(values))


def period_min(series, *, kpi, plan, spec, **_):
    """Smallest observed period value in the trailing window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    return None if not values else float(min(values))


def period_median(series, *, kpi, plan, spec, **_):
    """Median of observed period values in the trailing window."""
    values = sorted(v for _, v in _observed_pairs(series, kpi, plan, spec))
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return float(values[mid - 1] + values[mid]) / 2.0


def period_avg(series, *, kpi, plan, spec, **_):
    """Mean of observed period values in the trailing window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    return float(sum(values)) / float(len(values))


def period_sum(series, *, kpi, plan, spec, **_):
    """Sum of observed period values in the trailing window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    return None if not values else float(sum(values))


def _require_bar(spec) -> float:
    if spec.constant is None:
        raise CatalogError(f"hook {spec.hook!r} requires `value:`.")
    return float(spec.constant)


def hit_rate(series, *, kpi, plan, spec, **_):
    """Percent of observed periods whose value is >= value."""
    bar = _require_bar(spec)
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    hits = sum(1 for v in values if v >= bar)
    return float(hits) * 100.0 / float(len(values))


def streak(series, *, kpi, plan, spec, **_):
    """Consecutive hits >= value ending at the anchor. 0 if the anchor misses."""
    if kpi.time is None or plan is None or not spec.of:
        return None
    bar = _require_bar(spec)
    anchor = support.truncate_period_safe(plan.anchor, kpi)
    current = _at(series, kpi.time.column, spec.of, anchor)
    if current is None:
        return None
    dates = list(reversed(_window_dates(kpi, plan, spec)))
    count = 0
    for month in dates:
        value = _at(series, kpi.time.column, spec.of, month)
        if value is None:
            if month == anchor:
                return None
            break
        if value < bar:
            break
        count += 1
    return float(count)
