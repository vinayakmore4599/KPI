"""Measure functions for `measures.fn`. Register via registries/functions/measure.yaml."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kpi_engine.exceptions import CatalogError


def _fold(step: Callable[[Any, Any], Any], args: tuple[Any, ...]) -> Any:
    """Apply a two-argument step left to right."""
    result = args[0]
    for item in args[1:]:
        result = step(result, item)
    return result


def growth_pct(current: Any, previous: Any) -> float | None:
    """Relative change from `previous` to `current`. Zero or null base yields null."""
    if current is None or previous in (None, 0):
        return None
    return float((current - previous) / previous)


def divide_scalars(numerator: Any, denominator: Any) -> float | None:
    """Ratio. Zero or null denominator yields null."""
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator / denominator)


def percent_scalars(part: Any, whole: Any) -> float | None:
    """Share of `whole`, scaled to 0-100."""
    value = divide_scalars(part, whole)
    return None if value is None else float(value * 100)


def _numeric_fold(op: Callable[[Any, Any], Any]):
    """Wrap a two-scalar operation as a variadic fold; any null yields null."""

    def step(*values: Any) -> float | None:
        if any(value is None for value in values):
            return None
        return float(_fold(op, values))

    return step


def _numeric_pick(choose: Callable[[list[Any]], Any]):
    """Reduce non-null operands; null only when nothing is left."""

    def step(*values: Any) -> float | None:
        present = [value for value in values if value is not None]
        return None if not present else float(choose(present))

    return step


sum_scalars = _numeric_fold(lambda a, b: a + b)
subtract_scalars = _numeric_fold(lambda a, b: a - b)
multiply_scalars = _numeric_fold(lambda a, b: a * b)
min_scalars = _numeric_pick(min)
max_scalars = _numeric_pick(max)
avg_scalars = _numeric_pick(lambda vs: sum(vs) / len(vs))


def abs_scalar(value: Any) -> float | None:
    """Absolute value of one measure scalar."""
    if value is None:
        return None
    return abs(float(value))


def clamp_scalars(value: Any, lo: Any, hi: Any) -> float | None:
    """Clamp `value` into [lo, hi]. Any null yields null."""
    if value is None or lo is None or hi is None:
        return None
    return float(min(max(value, lo), hi))


def attainment(actual: Any, target: Any) -> float | None:
    """actual / target * 100. Null or zero target yields null."""
    return percent_scalars(actual, target)


def _is_null(value: Any) -> bool:
    """True when a measure scalar is missing."""
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def coalesce_scalars(*values: Any) -> float | None:
    """First non-null measure scalar."""
    for value in values:
        if not _is_null(value):
            return float(value)
    return None


def if_null_scalars(value: Any, fallback: Any) -> float | None:
    """`value` when present, otherwise `fallback`."""
    return coalesce_scalars(value, fallback)


def nullif_scalars(value: Any, sentinel: Any) -> float | None:
    """Null when `value` equals `sentinel`; otherwise `value`."""
    if _is_null(value):
        return None
    if _is_null(sentinel):
        return float(value)
    return None if float(value) == float(sentinel) else float(value)


def null_if_zero_scalars(value: Any) -> float | None:
    """Null when `value` is 0."""
    if _is_null(value):
        return None
    return None if float(value) == 0 else float(value)


def zero_if_null_scalars(value: Any) -> float | None:
    """0 when `value` is null."""
    return 0.0 if _is_null(value) else float(value)


def is_null_scalars(value: Any) -> float:
    """1 if `value` is null, else 0."""
    return 1.0 if _is_null(value) else 0.0


def is_not_null_scalars(value: Any) -> float:
    """1 if `value` is present, else 0."""
    return 0.0 if _is_null(value) else 1.0


def _is_true(cond: Any) -> bool:
    """Non-null and nonzero."""
    if _is_null(cond):
        return False
    return float(cond) != 0


def if_else_scalars(cond: Any, then: Any, other: Any) -> float | None:
    """`then` if `cond` is non-null and nonzero, otherwise `other`."""
    chosen = then if _is_true(cond) else other
    return None if _is_null(chosen) else float(chosen)


def sign_label(
    value: Any,
    positive: Any = "Positive",
    negative: Any = "Negative",
    neutral: Any = "Neutral",
) -> str | None:
    """Label the sign of a scalar. Null stays null; zero is Neutral."""
    if _is_null(value):
        return None
    number = float(value)
    if number > 0:
        return str(positive)
    if number < 0:
        return str(negative)
    return str(neutral)


def _finite_or_none(value: float) -> float | None:
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def round_scalar(value: Any, decimals: Any = 0) -> float | None:
    """Round a measure scalar. Null stays null."""
    if _is_null(value):
        return None
    places = 0 if _is_null(decimals) else int(float(decimals))
    return float(round(float(value), places))


def floor_scalar(value: Any) -> float | None:
    import math

    if _is_null(value):
        return None
    return float(math.floor(float(value)))


def ceil_scalar(value: Any) -> float | None:
    import math

    if _is_null(value):
        return None
    return float(math.ceil(float(value)))


def power_scalars(base: Any, exp: Any) -> float | None:
    if _is_null(base) or _is_null(exp):
        return None
    try:
        return _finite_or_none(float(base) ** float(exp))
    except Exception:
        return None


def log_scalar(value: Any) -> float | None:
    import math

    if _is_null(value) or float(value) <= 0:
        return None
    try:
        return _finite_or_none(math.log(float(value)))
    except Exception:
        return None


def log10_scalar(value: Any) -> float | None:
    import math

    if _is_null(value) or float(value) <= 0:
        return None
    try:
        return _finite_or_none(math.log10(float(value)))
    except Exception:
        return None


def sqrt_scalar(value: Any) -> float | None:
    import math

    if _is_null(value) or float(value) < 0:
        return None
    try:
        return _finite_or_none(math.sqrt(float(value)))
    except Exception:
        return None


def _reject_date_collection(value: Any, name: str) -> None:
    if isinstance(value, (list, tuple)):
        raise CatalogError(f"{name} cannot take a list or trend array.")


def _as_naive_timestamp(value: Any, name: str):
    """Parse a date-like scalar. Numbers are rejected (not Excel serials)."""
    import pandas as pd

    _reject_date_collection(value, name)
    if _is_null(value):
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise CatalogError(f"{name} needs a date, not a number ({value!r}).")
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        raise CatalogError(f"{name} requires tz-naive timestamps.")
    return ts


def _date_unit(unit: Any, name: str) -> str:
    kind = "day" if unit is None else str(unit).strip().lower()
    if kind not in {"day", "week", "month", "year"}:
        raise CatalogError(f"{name} unit must be day, week, month, or year (got {kind!r}).")
    return kind


def date_diff(start: Any, end: Any, unit: Any = "day") -> float | None:
    """end - start in day/week/month/year. Null in either side is null."""
    left = _as_naive_timestamp(start, "date_diff")
    right = _as_naive_timestamp(end, "date_diff")
    if left is None or right is None:
        return None
    kind = _date_unit(unit, "date_diff")
    if kind == "day":
        return float((right - left).days)
    if kind == "week":
        return float((right - left).days // 7)
    if kind == "month":
        return float((right.year - left.year) * 12 + (right.month - left.month))
    return float(right.year - left.year)


def date_add(value: Any, n: Any, unit: Any = "day") -> str | None:
    """Add n day/week/month/year units. Returns an ISO date string."""
    import pandas as pd

    stamp = _as_naive_timestamp(value, "date_add")
    _reject_date_collection(n, "date_add")
    if stamp is None or _is_null(n):
        return None
    kind = _date_unit(unit, "date_add")
    step = int(float(n))
    if kind == "day":
        out = stamp + pd.Timedelta(days=step)
    elif kind == "week":
        out = stamp + pd.Timedelta(weeks=step)
    elif kind == "month":
        out = stamp + pd.DateOffset(months=step)
    else:
        out = stamp + pd.DateOffset(years=step)
    ts = pd.Timestamp(out)
    return ts.normalize().date().isoformat()


def epoch_day(value: Any) -> float | None:
    """Integer days since 1970-01-01 (tz-naive date)."""
    import pandas as pd

    stamp = _as_naive_timestamp(value, "epoch_day")
    if stamp is None:
        return None
    origin = pd.Timestamp("1970-01-01")
    return float((stamp.normalize() - origin).days)
