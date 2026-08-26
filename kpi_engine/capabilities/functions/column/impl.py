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


def weighted_product_columns(*columns: pd.Series) -> pd.Series:
    """Row-wise product (alias used as weighted_product)."""
    return multiply_columns(*columns)


def safe_divide_columns(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Row-wise divide; zero/null denominator is null."""
    return divide_columns(numerator, denominator)


def clip_columns(value: pd.Series, lo: pd.Series, hi: pd.Series) -> pd.Series:
    """Clamp value into [lo, hi]."""
    numeric = pd.to_numeric(value, errors="coerce")
    low = pd.to_numeric(lo, errors="coerce")
    high = pd.to_numeric(hi, errors="coerce")
    return numeric.clip(lower=low, upper=high)


def parse_date_columns(value: pd.Series) -> pd.Series:
    """Parse strings/numbers into timestamps; invalid → null."""
    return pd.to_datetime(value, errors="coerce")


def parse_number_columns(value: pd.Series) -> pd.Series:
    """Parse strings into floats; invalid → null."""
    return pd.to_numeric(value, errors="coerce")


def _as_str(column: pd.Series) -> pd.Series:
    return column.astype("string")


def trim_columns(value: pd.Series) -> pd.Series:
    return _as_str(value).str.strip()


def upper_columns(value: pd.Series) -> pd.Series:
    return _as_str(value).str.upper()


def lower_columns(value: pd.Series) -> pd.Series:
    return _as_str(value).str.lower()


def substring_columns(value: pd.Series, start: pd.Series, length: pd.Series | None = None) -> pd.Series:
    text = _as_str(value)
    begin = pd.to_numeric(start, errors="coerce").fillna(0).astype(int)
    if length is None:
        return pd.Series(
            [t[s:] if pd.notna(t) else pd.NA for t, s in zip(text, begin)],
            index=value.index,
        )
    n = pd.to_numeric(length, errors="coerce").fillna(0).astype(int)
    return pd.Series(
        [t[s : s + k] if pd.notna(t) else pd.NA for t, s, k in zip(text, begin, n)],
        index=value.index,
    )


def left_columns(value: pd.Series, n: pd.Series) -> pd.Series:
    text = _as_str(value)
    count = pd.to_numeric(n, errors="coerce").fillna(0).astype(int)
    return pd.Series(
        [t[:k] if pd.notna(t) else pd.NA for t, k in zip(text, count)],
        index=value.index,
    )


def right_columns(value: pd.Series, n: pd.Series) -> pd.Series:
    text = _as_str(value)
    count = pd.to_numeric(n, errors="coerce").fillna(0).astype(int)
    return pd.Series(
        [t[-k:] if pd.notna(t) and k else (t if pd.notna(t) else pd.NA) for t, k in zip(text, count)],
        index=value.index,
    )


def replace_columns(value: pd.Series, old: pd.Series, new: pd.Series) -> pd.Series:
    text = _as_str(value)
    return pd.Series(
        [
            t.replace(str(a), str(b)) if pd.notna(t) else pd.NA
            for t, a, b in zip(text, old, new)
        ],
        index=value.index,
    )


def concat_columns(*columns: pd.Series) -> pd.Series:
    parts = [_as_str(c).fillna("") for c in columns]
    out = parts[0]
    for part in parts[1:]:
        out = out + part
    return out


def hash_bucket_columns(value: pd.Series, buckets: pd.Series | None = None) -> pd.Series:
    """Stable bucket 0..n-1 from the string form of value."""
    n = 10
    if buckets is not None:
        sample = pd.to_numeric(buckets, errors="coerce").dropna()
        if not sample.empty:
            n = max(int(sample.iloc[0]), 1)
    hashed = pd.util.hash_pandas_object(_as_str(value), index=False)
    return (hashed % n).astype("float64").mask(value.isna())


def json_extract_columns(value: pd.Series, path: pd.Series) -> pd.Series:
    """Pandas-only JSON path. path is like $.a.b; missing → null."""
    import json

    def one(raw: Any, spec: Any) -> Any:
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return pd.NA
        try:
            data = json.loads(str(raw)) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            return pd.NA
        text = "" if spec is None or (isinstance(spec, float) and pd.isna(spec)) else str(spec)
        keys = [p for p in text.replace("$", "").split(".") if p]
        cur = data
        for key in keys:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return pd.NA
        if isinstance(cur, (dict, list)):
            return json.dumps(cur)
        return cur

    return pd.Series([one(v, p) for v, p in zip(value, path)], index=value.index)


def coalesce_date_columns(*columns: pd.Series) -> pd.Series:
    """First non-null parseable date on each row."""
    parsed = [pd.to_datetime(c, errors="coerce") for c in columns]
    out = parsed[0]
    for part in parsed[1:]:
        out = out.fillna(part)
    return out


def is_between_dates_columns(value: pd.Series, start: pd.Series, end: pd.Series) -> pd.Series:
    stamp = pd.to_datetime(value, errors="coerce")
    lo = pd.to_datetime(start, errors="coerce")
    hi = pd.to_datetime(end, errors="coerce")
    flag = stamp.notna() & lo.notna() & hi.notna() & (stamp >= lo) & (stamp <= hi)
    return flag.astype("float64")


def flag_in_set_columns(value: pd.Series, *members: pd.Series) -> pd.Series:
    """1 when value equals any sibling/constant member on the row."""
    text = _as_str(value)
    flags = pd.Series(False, index=value.index)
    for member in members:
        flags = flags | text.eq(_as_str(member))
    return flags.astype("float64").mask(value.isna())
