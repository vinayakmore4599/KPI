"""Exception types for the KPI engine.

What this file provides
    A small hierarchy: KPIEngineError and subclasses for context, bind, filter,
    time-plan, and catalog failures.

Where it is used
    Raised from adapter, binder, filters, time_planner, model_sql, calc_engine.
    Tests assert on these types. Callers should treat any KPIEngineError as a
    failed request (do not retry blindly).

Capabilities
    Distinguishes "bad context" vs "unknown measure_key" vs "missing month
    filter" so logs and API error codes can stay precise.

When to use
    Catch KPIEngineError at the UDF boundary. Add a new subclass only when a
    new failure class needs different handling (not for one-off messages).
"""


class KPIEngineError(Exception):
    """Base error for bind, plan, and execution failures."""


class ContextError(KPIEngineError):
    """The request context is missing, malformed, or violates a locked rule."""


class BindError(KPIEngineError):
    """KPI YAML, model YAML, or dataset binding failed."""


class FilterError(KPIEngineError):
    """A filter cannot be applied safely (unmapped, hierarchy, empty contract)."""


class TimePlanError(KPIEngineError):
    """Anchor or lookback planning failed."""


class CatalogError(KPIEngineError):
    """An output op is illegal, unknown, or cannot be composed."""
