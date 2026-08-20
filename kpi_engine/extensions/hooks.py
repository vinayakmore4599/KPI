"""Allowlisted named hooks for logic the catalog cannot express.

What this file provides
    REGISTRY, register(name, fn), run(name, ...).

Where it is used
    Reserved for measures with op/hook in a later slice. First slice KPIs
    (3004) do not call hooks.

Capabilities
    YAML may only name keys in REGISTRY — never dotted import paths.

When to use
    Register a function here, then reference it from KPI YAML. Do not use
    importlib on context.udf.module_path for YAML KPIs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from kpi_engine.exceptions import CatalogError

Hook = Callable[..., pd.DataFrame]

REGISTRY: dict[str, Hook] = {}


def register(name: str, fn: Hook) -> None:
    """Register a named hook. KPI YAML may only call names present here."""
    REGISTRY[name] = fn


def run(name: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
    """Execute an allowlisted hook by name, or fail if it was never registered."""
    if name not in REGISTRY:
        raise CatalogError(
            f"Unknown hook {name!r}. Register it in kpi_engine.extensions.hooks.REGISTRY."
        )
    return REGISTRY[name](*args, **kwargs)
