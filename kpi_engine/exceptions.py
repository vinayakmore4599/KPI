"""Engine-level errors. Callers should treat these as request failures."""


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
