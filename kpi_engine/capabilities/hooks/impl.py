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


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in ys) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return float(num / (den_x * den_y))


def autocorrelation(series, *, kpi, plan, spec, **_):
    """Lag-1 Pearson correlation of consecutive observed period values."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if len(values) < 3:
        return None
    return _pearson(values[:-1], values[1:])


def rsi(series, *, kpi, plan, spec, **_):
    """Wilder RSI of period-to-period changes. Period = trailing length or 14."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if len(values) < 3:
        return None
    gains, losses = [], []
    for prev, cur in zip(values, values[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    period = min(len(gains), int(spec.params.get("n") or spec.trailing_months or 14))
    if period < 1:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def bollinger(series, *, kpi, plan, spec, **_):
    """%b of the last observed value in a k-sigma band around the window mean."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    stdev = var ** 0.5
    k = float(spec.params.get("k") or 2)
    width = 2.0 * k * stdev
    if width == 0:
        return None
    last = values[-1]
    return float((last - (mean - k * stdev)) / width)


def exponential_decay_sum(series, *, kpi, plan, spec, **_):
    """Sum v_i * decay^(n-1-i) from oldest to newest. decay default 0.5."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if not pairs:
        return None
    decay = float(spec.params.get("decay") or 0.5)
    acc = 0.0
    for i, (_, value) in enumerate(reversed(pairs)):
        acc += float(value) * (decay ** i)
    return float(acc)


def theil_sen_slope(series, *, kpi, plan, spec, **_):
    """Median of pairwise slopes of value vs 0..n-1 observed index."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    n = len(pairs)
    if n < 2:
        return None
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = float(j - i)
            if dx == 0:
                continue
            slopes.append((pairs[j][1] - pairs[i][1]) / dx)
    if not slopes:
        return None
    slopes.sort()
    mid = len(slopes) // 2
    if len(slopes) % 2:
        return float(slopes[mid])
    return float(slopes[mid - 1] + slopes[mid]) / 2.0


def changepoint(series, *, kpi, plan, spec, **_):
    """1-based index of the split that maximises |mean(left) − mean(right)|."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    n = len(values)
    if n < 3:
        return None
    best_i, best_score = 1, -1.0
    for i in range(1, n):
        left = values[:i]
        right = values[i:]
        if not left or not right:
            continue
        score = abs(sum(left) / len(left) - sum(right) / len(right))
        if score > best_score:
            best_score = score
            best_i = i
    return float(best_i)


def outlier_count(series, *, kpi, plan, spec, **_):
    """Count of observed values with |z| greater than k (default 3)."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    stdev = var ** 0.5
    if stdev == 0:
        return 0.0
    k = float(spec.params.get("k") or 3)
    return float(sum(1 for v in values if abs((v - mean) / stdev) > k))


def percentile_rank_series(series, *, kpi, plan, spec, **_):
    """Percentile rank (0–100) of the last observed value in the window."""
    values = [v for _, v in _observed_pairs(series, kpi, plan, spec)]
    if not values:
        return None
    last = values[-1]
    below = sum(1 for v in values if v < last)
    equal = sum(1 for v in values if v == last)
    return float((below + 0.5 * equal) / len(values) * 100.0)


def weighted_ewma(series, *, kpi, plan, spec, **_):
    """EWMA with explicit alpha (default 2/(N+1), same as hook:ewma)."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if not pairs:
        return None
    n = spec.trailing_months or len(pairs)
    alpha = spec.params.get("alpha")
    alpha = 2.0 / (n + 1) if alpha is None else float(alpha)
    value = pairs[0][1]
    for _, item in pairs[1:]:
        value = alpha * item + (1 - alpha) * value
    return float(value)


def run_rate(series, *, kpi, plan, spec, **_):
    """Last observed value annualized by grain (month×12, quarter×4, …)."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if not pairs or kpi.time is None:
        return None
    per_year = {"day": 365, "week": 52, "month": 12, "quarter": 4, "year": 1}[kpi.time.grain]
    return float(pairs[-1][1]) * float(per_year)


def target_trajectory(series, *, kpi, plan, spec, **_):
    """Last / linear expected path from first observed to `value:` target."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if len(pairs) < 2 or spec.constant is None:
        return None
    first = pairs[0][1]
    target = float(spec.constant)
    n = len(pairs)
    expected = first + (target - first) * ((n - 1) / max(n - 1, 1))
    if expected == 0:
        return None
    return float(pairs[-1][1]) / expected


def forecast_confidence(series, *, kpi, plan, spec, **_):
    """Linear projection ± k × sample stdev. Dual keys via bind `side: low|high`."""
    pairs = _observed_pairs(series, kpi, plan, spec)
    if len(pairs) < 2:
        return None
    pred = projection(series, kpi=kpi, plan=plan, spec=spec)
    if pred is None:
        return None
    values = [v for _, v in pairs]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    stdev = var ** 0.5
    k = float(spec.params.get("k") or 1.96)
    half = k * stdev
    side = str(spec.params.get("side") or "high").strip().lower()
    return float(pred - half) if side == "low" else float(pred + half)


def seasonal_decompose(series, *, kpi, plan, spec, **_):
    """Seasonal factor at the anchor month: same-month mean / overall mean."""
    if kpi.time is None or plan is None or not spec.of:
        return None
    anchor = support.truncate_period_safe(plan.anchor, kpi)
    pairs = _observed_pairs(series, kpi, plan, spec)
    if not pairs:
        return None
    month_vals = [value for month, value in pairs if month.month == anchor.month]
    overall = [value for _, value in pairs]
    if not month_vals or not overall:
        return None
    overall_mean = sum(overall) / len(overall)
    if overall_mean == 0:
        return None
    return float(sum(month_vals) / len(month_vals) / overall_mean)


def _parse_entry_period(raw: Any, kpi, plan) -> date | None:
    if raw is None:
        return plan.anchor if plan is not None else None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _combo_detail(detail, combo, group_dims):
    if detail is None or detail.empty:
        return None
    work = detail
    if combo is not None:
        for dim in group_dims or []:
            if dim in work.columns and dim in combo.index:
                work = work[work[dim] == combo[dim]]
    return work


def _cohort_ids(work, kpi, col: str) -> pd.Series:
    ts = pd.to_datetime(work[kpi.time.column]).dt.normalize()
    return work.assign(_t=ts).groupby(col)["_t"].min()


def cohort_retention(series, *, kpi, plan, spec, detail=None, combo=None, group_dims=None, **_):
    """Share of entities whose first period equals entry_period that are still at the anchor."""
    del series
    if detail is None or kpi.time is None or plan is None:
        return None
    col = spec.params.get("cohort_column")
    work = _combo_detail(detail, combo, group_dims)
    if not col or work is None or work.empty or col not in work.columns:
        return None
    entry = _parse_entry_period(spec.params.get("entry_period"), kpi, plan)
    if entry is None:
        return None
    entries = _cohort_ids(work, kpi, col)
    wanted = pd.Timestamp(support.truncate_period_safe(entry, kpi))
    cohort = entries[entries == wanted]
    if cohort.empty:
        return None
    ids = set(cohort.index)
    ts = pd.to_datetime(work[kpi.time.column]).dt.normalize()
    anchor = pd.Timestamp(support.truncate_period_safe(plan.anchor, kpi))
    present = set(work.loc[ts == anchor, col])
    return float(len(ids & present) / len(ids))


def survival_rate(series, *, kpi, plan, spec, detail=None, combo=None, group_dims=None, **_):
    """Share of entities that entered on or before entry_period still present at the anchor."""
    del series
    if detail is None or kpi.time is None or plan is None:
        return None
    col = spec.params.get("cohort_column")
    work = _combo_detail(detail, combo, group_dims)
    if not col or work is None or work.empty or col not in work.columns:
        return None
    entry = _parse_entry_period(spec.params.get("entry_period"), kpi, plan)
    if entry is None:
        return None
    entries = _cohort_ids(work, kpi, col)
    wanted = pd.Timestamp(support.truncate_period_safe(entry, kpi))
    cohort = entries[entries <= wanted]
    if cohort.empty:
        return None
    ids = set(cohort.index)
    ts = pd.to_datetime(work[kpi.time.column]).dt.normalize()
    anchor = pd.Timestamp(support.truncate_period_safe(plan.anchor, kpi))
    present = set(work.loc[ts == anchor, col])
    return float(len(ids & present) / len(ids))

