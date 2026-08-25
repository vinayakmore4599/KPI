"""Named series hooks. Register via registries/hooks.yaml."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from kpi_engine.capabilities.ops import support
from kpi_engine.dates import period_range_inclusive, periods_between
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


def period_stdev(series, *, kpi, plan, spec, **_):
    """Sample standard deviation of observed period values."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    moments = support.sample_mean_var(values)
    return None if moments is None else float(moments[1] ** 0.5)


def period_var(series, *, kpi, plan, spec, **_):
    """Sample variance of observed period values."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    moments = support.sample_mean_var(values)
    return None if moments is None else float(moments[1])


def period_cv(series, *, kpi, plan, spec, **_):
    """Sample stdev / mean × 100. Null when mean is 0 or fewer than two values."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    moments = support.sample_mean_var(values)
    if moments is None or moments[0] == 0:
        return None
    return float(moments[1] ** 0.5) * 100.0 / float(moments[0])


def period_range(series, *, kpi, plan, spec, **_):
    """Largest minus smallest observed period value."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    return float(max(values) - min(values))


def period_count(series, *, kpi, plan, spec, **_):
    """Count of observed (non-null) periods in the trailing window."""
    return float(len(_observed_pairs(series, kpi, plan, spec)))


def miss_rate(series, *, kpi, plan, spec, **_):
    """Percent of observed periods whose value is below value."""
    bar = _require_bar(spec)
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    misses = sum(1 for v in values if v < bar)
    return float(misses) * 100.0 / float(len(values))


def miss_streak(series, *, kpi, plan, spec, **_):
    """Consecutive misses (value < bar) ending at the anchor. 0 if the anchor hits."""
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
        if value >= bar:
            break
        count += 1
    return float(count)


def longest_streak(series, *, kpi, plan, spec, **_):
    """Longest run of observed periods >= value anywhere in the window."""
    bar = _require_bar(spec)
    best = 0
    run = 0
    for _, value in _observed_pairs(series, kpi, plan, spec):
        if value >= bar:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return float(best)


def cagr(series, *, kpi, plan, spec, **_):
    """Compound annual growth from first to last observed value (same scale as growth_pct)."""
    if kpi.time is None:
        return None
    pairs = _observed_pairs(series, kpi, plan, spec)
    if len(pairs) < 2:
        return None
    first_date, first = pairs[0]
    last_date, last = pairs[-1]
    if first in (None, 0) or last is None:
        return None
    steps = periods_between(first_date, last_date, kpi.time)
    if steps == 0:
        return None
    per_year = {"day": 365, "week": 52, "month": 12, "quarter": 4, "year": 1}[kpi.time.grain]
    years = float(steps) / float(per_year)
    return float(last / first) ** (1.0 / years) - 1.0


def slope(series, *, kpi, plan, spec, **_):
    """Least-squares slope of value vs 0..n-1 observed period index."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    n = len(pairs)
    if n < 2:
        return None
    xs = list(range(n))
    ys = [value for _, value in pairs]
    sum_x = float(sum(xs))
    sum_y = float(sum(ys))
    sum_xx = float(sum(x * x for x in xs))
    sum_xy = float(sum(x * y for x, y in zip(xs, ys)))
    den = n * sum_xx - sum_x * sum_x
    if den == 0:
        return None
    return (n * sum_xy - sum_x * sum_y) / den


def mad(series, *, kpi, plan, spec, **_):
    """Median absolute deviation of observed period values in the trailing window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        centre = float(ordered[mid])
    else:
        centre = float(ordered[mid - 1] + ordered[mid]) / 2.0
    deviations = sorted(abs(v - centre) for v in values)
    dmid = len(deviations) // 2
    if len(deviations) % 2:
        return float(deviations[dmid])
    return float(deviations[dmid - 1] + deviations[dmid]) / 2.0


def projection(series, *, kpi, plan, spec, **_):
    """Linear forecast: last observed value + slope * periods_ahead (default 1)."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if len(pairs) < 2:
        return None
    fitted = slope(series, kpi=kpi, plan=plan, spec=spec)
    if fitted is None:
        return None
    ahead = spec.params.get("periods_ahead", 1) if spec.params else 1
    try:
        ahead = int(ahead)
    except (TypeError, ValueError):
        ahead = 1
    last = pairs[-1][1]
    return float(last) + float(fitted) * float(ahead)
