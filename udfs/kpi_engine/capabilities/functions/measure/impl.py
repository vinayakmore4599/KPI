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
