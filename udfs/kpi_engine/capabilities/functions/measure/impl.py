"""Measure functions for `measures.fn`. Register via registries/functions/measure.yaml."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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
