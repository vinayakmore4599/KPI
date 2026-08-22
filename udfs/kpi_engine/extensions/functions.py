"""Extension point for project-specific catalog functions.

What this file provides
    register_column_fn / register_measure_fn and their unregister pairs, plus
    read-only views of the two registries.

Where it is used
    Import this module and register a function, then name it from KPI YAML:
    `base_measures.*.op` resolves in COLUMN_FNS, `measures.*.fn` in MEASURE_FNS.

Capabilities
    Column functions receive one numeric pandas Series per entry in `columns:`
    and return a Series of the same length. Measure functions receive one
    scalar per entry in `inputs:` and return one scalar (None for undefined).
    How many arguments a function takes, and what they may be called from YAML,
    is read off its Python signature: `def rate(shipped, ordered)` accepts
    exactly two and can be fed by name, `def total(*columns)` accepts any
    number and needs min_columns to say how few are too few.

When to use
    Do not add names here. Add the function under capabilities/functions/ and
    a row under registries/functions/. Built-in names are already allowlisted.
"""

from __future__ import annotations

import warnings
from types import MappingProxyType
from typing import Mapping

warnings.warn(
    "kpi_engine.extensions.functions is a compatibility shim. "
    "Add functions under capabilities/functions/ and registries/functions/. "
    "This import path will be removed in the next release.",
    DeprecationWarning,
    stacklevel=2,
)

from kpi_engine.core.fn_apply import (
    COLUMN_FNS,
    MEASURE_FNS,
    ColumnFn,
    FnMeta,
    MeasureFn,
    column_fn_meta,
    measure_fn_meta,
    register_column_fn,
    register_measure_fn,
    unregister_column_fn,
    unregister_measure_fn,
)

__all__ = [
    "ColumnFn",
    "FnMeta",
    "MeasureFn",
    "column_fn_meta",
    "column_functions",
    "measure_fn_meta",
    "measure_functions",
    "register_column_fn",
    "register_measure_fn",
    "unregister_column_fn",
    "unregister_measure_fn",
]


def column_functions() -> Mapping[str, ColumnFn]:
    """Every column function KPI YAML may name in `op:`."""
    return MappingProxyType(COLUMN_FNS)


def measure_functions() -> Mapping[str, MeasureFn]:
    """Every measure function KPI YAML may name in `fn:`."""
    return MappingProxyType(MEASURE_FNS)
