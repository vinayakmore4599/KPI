"""Canonical filter operators shared by DuckDB extract and Pandas calc/result.

What this file provides
    canonicalize_op — YAML aliases → one op name.
    assert_filter_arity — bind-time value count.
    sql_predicate — parameterized DuckDB fragment.
    pandas_mask — boolean Series with SQL null semantics.

Where it is used
    binder.parse of KPI `filters:`. model_sql._where_clause. filters.apply_*.

Capabilities
    Same op set at extract, calc, and result so the three stages cannot drift.
    Null column values fail every comparison except is_null / is_not_null.

When to use
    Add an operator here first, then YAML and tests. Do not special-case SQL
    or Pandas in the callers.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from kpi_engine.exceptions import BindError


FILTER_OP_HELP = (
    "in, eq (==, =, equals), ne (<>, !=, not_equals), "
    "lt (<), lte (<=, le), gt (>), gte (>=, ge), "
    "like (LIKE), ilike (ILIKE), not_like (NOT LIKE, notlike), "
    "between (BETWEEN), not_between (NOT BETWEEN), "
    "is_null (IS NULL, isnull), is_not_null (IS NOT NULL, notnull), "
    "regexp, regexp_insensitive"
)
REGEXP_MAX_LEN = 256
REGEXP_OPS = frozenset({"regexp", "regexp_insensitive"})

# Canonical op → expected value count. None means 0+ (IN).
FILTER_ARITY: dict[str, int | None] = {
    "in": None,
    "eq": 1,
    "ne": 1,
    "lt": 1,
    "lte": 1,
    "gt": 1,
    "gte": 1,
    "like": 1,
    "ilike": 1,
    "not_like": 1,
    "between": 2,
    "not_between": 2,
    "is_null": 0,
    "is_not_null": 0,
    "regexp": 1,
    "regexp_insensitive": 1,
}

_OP_ALIASES = {
    "in": "in",
    "eq": "eq",
    "==": "eq",
    "=": "eq",
    "equals": "eq",
    "ne": "ne",
    "<>": "ne",
    "!=": "ne",
    "not_equals": "ne",
    "lt": "lt",
    "<": "lt",
    "lte": "lte",
    "<=": "lte",
    "le": "lte",
    "gt": "gt",
    ">": "gt",
    "gte": "gte",
    ">=": "gte",
    "ge": "gte",
    "like": "like",
    "ilike": "ilike",
    "not_like": "not_like",
    "not like": "not_like",
    "notlike": "not_like",
    "between": "between",
    "not_between": "not_between",
    "not between": "not_between",
    "is_null": "is_null",
    "is null": "is_null",
    "isnull": "is_null",
    "is_not_null": "is_not_null",
    "is not null": "is_not_null",
    "notnull": "is_not_null",
    "regexp": "regexp",
    "regex": "regexp",
    "regexp_insensitive": "regexp_insensitive",
    "iregexp": "regexp_insensitive",
    "regex_insensitive": "regexp_insensitive",
}


def canonicalize_op(raw: Any) -> str:
    """Map a YAML op (symbol or name, any case) to one canonical name."""
    if raw is None or str(raw).strip() == "":
        return "in"
    text = str(raw).strip()
    folded = " ".join(text.lower().split())
    hit = _OP_ALIASES.get(folded) or _OP_ALIASES.get(folded.replace(" ", "_"))
    if hit is None:
        raise BindError(f"Unknown filter op {raw!r}. Use one of: {FILTER_OP_HELP}.")
    return hit


def assert_filter_arity(op: str, values: tuple[Any, ...], *, code: str) -> None:
    """Fail when the context value count does not match the operator."""
    expected = FILTER_ARITY[op]
    if expected is None:
        return
    got = len(values)
    if got != expected:
        raise BindError(
            f"Filter {code!r} op {op!r} expects {expected} value(s), got {got}."
        )


def assert_regexp_pattern(pattern: Any, *, code: str) -> str:
    """Bind-time regexp length and compile check (Pandas ``re``, max 256)."""
    text = str(pattern)
    if len(text) > REGEXP_MAX_LEN:
        raise BindError(
            f"{code} regexp pattern exceeds {REGEXP_MAX_LEN} characters."
        )
    try:
        re.compile(text)
    except re.error as exc:
        raise BindError(f"{code} invalid regexp: {exc}.") from exc
    return text


def sql_predicate(col_sql: str, op: str, values: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """Return a parameterized DuckDB predicate and the bound values."""
    if op == "in":
        if not values:
            return "FALSE", []
        placeholders = ", ".join("?" for _ in values)
        return f"{col_sql} IN ({placeholders})", list(values)
    if op == "eq":
        return f"{col_sql} = ?", [values[0]]
    if op == "ne":
        return f"{col_sql} <> ?", [values[0]]
    if op == "lt":
        return f"{col_sql} < ?", [values[0]]
    if op == "lte":
        return f"{col_sql} <= ?", [values[0]]
    if op == "gt":
        return f"{col_sql} > ?", [values[0]]
    if op == "gte":
        return f"{col_sql} >= ?", [values[0]]
    if op == "like":
        return f"{col_sql} LIKE ?", [values[0]]
    if op == "ilike":
        return f"{col_sql} ILIKE ?", [values[0]]
    if op == "not_like":
        return f"{col_sql} NOT LIKE ?", [values[0]]
    if op == "between":
        return f"{col_sql} BETWEEN ? AND ?", [values[0], values[1]]
    if op == "not_between":
        return f"{col_sql} NOT BETWEEN ? AND ?", [values[0], values[1]]
    if op == "is_null":
        return f"{col_sql} IS NULL", []
    if op == "is_not_null":
        return f"{col_sql} IS NOT NULL", []
    if op == "regexp":
        return f"regexp_matches({col_sql}, ?)", [values[0]]
    if op == "regexp_insensitive":
        return f"regexp_matches({col_sql}, ?, 'i')", [values[0]]
    raise BindError(f"Unknown filter op {op!r}. Use one of: {FILTER_OP_HELP}.")


def pandas_mask(series: pd.Series, op: str, values: tuple[Any, ...]) -> pd.Series:
    """Boolean mask; SQL-style: nulls do not pass comparison ops."""
    if op == "in":
        if not values:
            return pd.Series(False, index=series.index)
        return series.isin(list(values))
    if op == "eq":
        return series.eq(values[0])
    if op == "ne":
        return series.ne(values[0]) & series.notna()
    if op == "lt":
        return series.lt(values[0])
    if op == "lte":
        return series.le(values[0])
    if op == "gt":
        return series.gt(values[0])
    if op == "gte":
        return series.ge(values[0])
    if op == "like":
        return _like_mask(series, values[0], case=True, invert=False)
    if op == "ilike":
        return _like_mask(series, values[0], case=False, invert=False)
    if op == "not_like":
        return _like_mask(series, values[0], case=True, invert=True)
    if op == "between":
        return series.ge(values[0]) & series.le(values[1])
    if op == "not_between":
        return series.notna() & ~(series.ge(values[0]) & series.le(values[1]))
    if op == "is_null":
        return series.isna()
    if op == "is_not_null":
        return series.notna()
    if op == "regexp":
        return _regexp_mask(series, values[0], case=True)
    if op == "regexp_insensitive":
        return _regexp_mask(series, values[0], case=False)
    raise BindError(f"Unknown filter op {op!r}. Use one of: {FILTER_OP_HELP}.")


def like_pattern_to_regex(pattern: str) -> str:
    """SQL LIKE → regex: % any run, _ any char; other characters are literal."""
    parts = ["^"]
    for char in str(pattern):
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    parts.append("$")
    return "".join(parts)


def _like_mask(series: pd.Series, pattern: Any, *, case: bool, invert: bool) -> pd.Series:
    """Match SQL LIKE / ILIKE / NOT LIKE; nulls never pass."""
    regex = like_pattern_to_regex(str(pattern))
    flags = 0 if case else re.IGNORECASE
    text = series.astype("string")
    matched = text.str.contains(regex, regex=True, flags=flags, na=False)
    if invert:
        return series.notna() & ~matched
    return matched


def _regexp_mask(series: pd.Series, pattern: Any, *, case: bool) -> pd.Series:
    """Match regexp / regexp_insensitive; non-string columns astype string; nulls never pass."""
    flags = 0 if case else re.IGNORECASE
    text = series.astype("string")
    return text.str.contains(str(pattern), regex=True, flags=flags, na=False)
