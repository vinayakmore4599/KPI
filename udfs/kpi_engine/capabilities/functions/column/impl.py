"""Column functions for `base_measures.op`. Register via registries/functions/column.yaml."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from kpi_engine.exceptions import CatalogError


def _fold(step: Callable[[Any, Any], Any], args: tuple[Any, ...]) -> Any:
    """Apply a two-argument step left to right."""
    result = args[0]
    for item in args[1:]:
        result = step(result, item)
    return result


def _side_by_side(columns: tuple[pd.Series, ...]) -> pd.DataFrame:
    """Line operands up as columns for a row-wise reducer."""
    return pd.concat(columns, axis=1)


def value(column: pd.Series) -> pd.Series:
    """Pass one column through unchanged."""
    return column


def abs_columns(column: pd.Series) -> pd.Series:
    """Absolute value of one column."""
    return column.abs()


def sum_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise sum of two or more columns."""
    return _fold(lambda a, b: a + b, columns)


def subtract_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise left-to-right subtraction."""
    return _fold(lambda a, b: a - b, columns)


def multiply_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise product of two or more columns."""
    return _fold(lambda a, b: a * b, columns)


def divide_columns(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Row-wise divide. Zero denominator is null."""
    return numerator / denominator.replace(0, pd.NA)


def percent_of_columns(part: pd.Series, whole: pd.Series) -> pd.Series:
    """Share of `whole`, scaled to 0-100."""
    return divide_columns(part, whole) * 100


def min_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise minimum."""
    return _side_by_side(columns).min(axis=1)


def max_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise maximum."""
    return _side_by_side(columns).max(axis=1)


def avg_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise mean."""
    return _side_by_side(columns).mean(axis=1)


def coalesce_columns(*columns: pd.Series) -> pd.Series:
    """First non-null value on each row."""
    return _fold(lambda a, b: a.fillna(b), columns)


def if_null_columns(value: pd.Series, fallback: pd.Series) -> pd.Series:
    """`value` when present, otherwise `fallback`."""
    return value.fillna(fallback)


def nullif_columns(value: pd.Series, sentinel: pd.Series) -> pd.Series:
    """Null where `value` equals `sentinel`; otherwise `value`."""
    return value.where(~value.eq(sentinel))


def null_if_zero_columns(value: pd.Series) -> pd.Series:
    """Null where `value` is 0."""
    numeric = pd.to_numeric(value, errors="coerce")
    return numeric.mask(numeric.eq(0))


def zero_if_null_columns(value: pd.Series) -> pd.Series:
    """0 where `value` is null."""
    return pd.to_numeric(value, errors="coerce").fillna(0)


def is_null_columns(value: pd.Series) -> pd.Series:
    """1 where `value` is null, else 0."""
    return value.isna().astype("float64")


def is_not_null_columns(value: pd.Series) -> pd.Series:
    """1 where `value` is present, else 0."""
    return value.notna().astype("float64")


def if_else_columns(cond: pd.Series, then: pd.Series, other: pd.Series) -> pd.Series:
    """`then` where `cond` is non-null and nonzero, otherwise `other`."""
    numeric = pd.to_numeric(cond, errors="coerce")
    pick = numeric.notna() & (numeric != 0)
    return then.where(pick, other)


def _numeric(column: pd.Series) -> pd.Series:
    return pd.to_numeric(column, errors="coerce")


def round_columns(value: pd.Series, decimals: pd.Series | None = None) -> pd.Series:
    """Round a numeric column. Optional decimals defaults to 0."""
    numeric = _numeric(value)
    if decimals is None:
        return numeric.round()
    places = _numeric(decimals)
    if places.nunique(dropna=True) == 1 and places.notna().any():
        return numeric.round(int(places.dropna().iloc[0]))
    out = [
        None if pd.isna(v) or pd.isna(n) else round(float(v), int(n))
        for v, n in zip(numeric, places)
    ]
    return pd.Series(out, index=value.index, dtype="float64")


def floor_columns(value: pd.Series) -> pd.Series:
    """Floor of one numeric column."""
    import math

    return _numeric(value).map(lambda v: None if pd.isna(v) else float(math.floor(v)))


def ceil_columns(value: pd.Series) -> pd.Series:
    """Ceiling of one numeric column."""
    import math

    return _numeric(value).map(lambda v: None if pd.isna(v) else float(math.ceil(v)))


def power_columns(base: pd.Series, exp: pd.Series) -> pd.Series:
    """base ** exp. Domain errors (inf/nan) are null."""
    import numpy as np

    a = _numeric(base).astype("float64")
    b = _numeric(exp).astype("float64")
    with np.errstate(all="ignore"):
        out = np.power(a.to_numpy(), b.to_numpy())
    result = pd.Series(out, index=base.index, dtype="float64")
    return result.mask(~np.isfinite(result))


def log_columns(value: pd.Series) -> pd.Series:
    """Natural log. Non-positive is null."""
    import numpy as np

    numeric = _numeric(value).astype("float64")
    with np.errstate(all="ignore"):
        out = np.log(numeric.to_numpy())
    result = pd.Series(out, index=value.index, dtype="float64")
    result = result.mask(numeric <= 0)
    return result.mask(~np.isfinite(result))


def log10_columns(value: pd.Series) -> pd.Series:
    """Base-10 log. Non-positive is null."""
    import numpy as np

    numeric = _numeric(value).astype("float64")
    with np.errstate(all="ignore"):
        out = np.log10(numeric.to_numpy())
    result = pd.Series(out, index=value.index, dtype="float64")
    result = result.mask(numeric <= 0)
    return result.mask(~np.isfinite(result))


def sqrt_columns(value: pd.Series) -> pd.Series:
    """Square root. Negative is null."""
    import numpy as np

    numeric = _numeric(value).astype("float64")
    with np.errstate(all="ignore"):
        out = np.sqrt(numeric.to_numpy())
    result = pd.Series(out, index=value.index, dtype="float64")
    result = result.mask(numeric < 0)
    return result.mask(~np.isfinite(result))


def _naive_datetime(series: pd.Series, *, name: str) -> pd.Series:
    ts = pd.to_datetime(series, errors="coerce")
    tz = getattr(getattr(ts, "dt", None), "tz", None)
    if tz is not None:
        raise CatalogError(f"{name} requires tz-naive timestamps.")
    return ts


def _unit_name(unit: pd.Series | str | None) -> str:
    if unit is None:
        return "day"
    if isinstance(unit, str):
        return unit.strip().lower()
    present = unit.dropna()
    if present.empty:
        return "day"
    return str(present.iloc[0]).strip().lower()


def date_diff_columns(
    start: pd.Series, end: pd.Series, unit: pd.Series | str | None = None
) -> pd.Series:
    """end - start in day/week/month/year. Null in either side is null."""
    left = _naive_datetime(start, name="date_diff")
    right = _naive_datetime(end, name="date_diff")
    kind = _unit_name(unit)
    allowed = {"day", "week", "month", "year"}
    if kind not in allowed:
        raise CatalogError(f"date_diff unit must be day, week, month, or year (got {kind!r}).")
    if kind == "day":
        delta = (right - left).dt.days.astype("float64")
    elif kind == "week":
        delta = ((right - left).dt.days // 7).astype("float64")
    elif kind == "month":
        delta = (
            (right.dt.year - left.dt.year) * 12 + (right.dt.month - left.dt.month)
        ).astype("float64")
    else:
        delta = (right.dt.year - left.dt.year).astype("float64")
    return delta.mask(left.isna() | right.isna())


def date_add_columns(
    value: pd.Series, n: pd.Series, unit: pd.Series | str | None = None
) -> pd.Series:
    """Add n day/week/month/year units to a date. Null in is null."""
    stamp = _naive_datetime(value, name="date_add")
    amount = _numeric(n)
    kind = _unit_name(unit)
    if kind not in {"day", "week", "month", "year"}:
        raise CatalogError(f"date_add unit must be day, week, month, or year (got {kind!r}).")
    shifted: list[Any] = []
    for ts, add in zip(stamp, amount):
        if pd.isna(ts) or pd.isna(add):
            shifted.append(pd.NaT)
            continue
        step = int(add)
        if kind == "day":
            shifted.append(ts + pd.Timedelta(days=step))
        elif kind == "week":
            shifted.append(ts + pd.Timedelta(weeks=step))
        elif kind == "month":
            shifted.append(ts + pd.DateOffset(months=step))
        else:
            shifted.append(ts + pd.DateOffset(years=step))
    return pd.Series(shifted, index=value.index)


def epoch_day_columns(value: pd.Series) -> pd.Series:
    """Integer days since 1970-01-01 (tz-naive date)."""
    stamp = _naive_datetime(value, name="epoch_day")
    origin = pd.Timestamp("1970-01-01")
    days = (stamp.dt.normalize() - origin).dt.days.astype("float64")
    return days.mask(stamp.isna())
