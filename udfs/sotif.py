"""Thin UDF shim for Sotif / kpi_id routing.

What this file provides
    main(context, config_dir=None) → same JSON as kpi_engine.compute.

Where it is used
    Existing metadata UDF (udf_name sotif, module_path udfs.sotif.main).
    Tests compare main() to compute() for equality.

Capabilities
    No Sotif-specific math. kpi_id in the context selects config/kpis/<id>.yaml.

When to use
    Keep this file a one-liner. Put calculation changes in YAML or calc_engine,
    not here.
"""

from __future__ import annotations

from typing import Any

from kpi_engine import compute


def main(context: dict[str, Any], *, config_dir: str | None = None) -> dict[str, Any]:
    """UDF entry the metadata layer calls. Forwards the context to the generic engine."""
    return compute(context, config_dir=config_dir)
