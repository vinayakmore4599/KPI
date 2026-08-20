"""Thin UDF shim. Metadata keeps calling udfs.sotif.main; routing is by kpi_id."""

from __future__ import annotations

from typing import Any

from kpi_engine import compute


def main(context: dict[str, Any], *, config_dir: str | None = None) -> dict[str, Any]:
    return compute(context, config_dir=config_dir)
