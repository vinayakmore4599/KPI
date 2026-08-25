"""Public API for the KPI engine.

What this file provides
    `compute(context)` — full request: bind YAML, DuckDB extract, Pandas calc, JSON.
    `validate(context)` — bind and compile SQL without scanning data (CI / dry-run).

Where it is used
    Platform UDF (`kpi_engine.main`), notebooks, and tests import from here so
    callers never need to know pipeline module names.

Capabilities
    Single entry for any kpi_id once YAML exists under kpi_config/kpis/ (optional
    one-level kpi_group folder), or KPI_ENGINE_CONFIG_DIR.

When to use
    Always call `compute` / `validate` from this package, not from orchestrator
    directly. Change this file only when adding a new public function.
"""

from kpi_engine._bootstrap import ensure_parent_on_path

ensure_parent_on_path()

from kpi_engine.pipeline.loader import ensure_loaded, list_capabilities
from kpi_engine.pipeline.orchestrator import compute, validate

ensure_loaded()

__all__ = ["compute", "validate", "list_capabilities"]
