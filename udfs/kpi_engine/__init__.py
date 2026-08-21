"""Public API for the KPI engine.

What this file provides
    `compute(context)` — full request: bind YAML, DuckDB extract, Pandas calc, JSON.
    `validate(context)` — bind and compile SQL without scanning data (CI / dry-run).

Where it is used
    Platform UDF (`udfs.sotif.main`), notebooks, and tests import from here so
    callers never need to know core module names.

Capabilities
    Single entry for any kpi_id once YAML exists under config/kpis/.

When to use
    Always call `compute` / `validate` from this package, not from orchestrator
    directly. Change this file only when adding a new public function.
"""

from kpi_engine.core.orchestrator import compute, validate

__all__ = ["compute", "validate"]
