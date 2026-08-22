"""Column functions for `base_measures.op`. Register via registries/functions/column.yaml."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd


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
