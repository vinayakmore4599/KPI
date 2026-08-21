"""Allowlisted named hooks for logic the catalog cannot express.

What this file provides
    REGISTRY, register(name, fn), run(name, ...).

Where it is used
    calc_engine.evaluate for measures with op: hook. YAML `hook:` must match
    a key in REGISTRY — never a dotted import path.

Capabilities
    Hooks receive the densified monthly series plus kpi/plan/spec and return
    a scalar (or a trend (axis, values) tuple). 3004 does not call hooks.

When to use
    Register a function here, then reference it from KPI YAML. Do not use
    importlib on context.udf.module_path for YAML KPIs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kpi_engine.exceptions import CatalogError

Hook = Callable[..., Any]

REGISTRY: dict[str, Hook] = {}


def register(name: str, fn: Hook) -> None:
    """Register a named hook. KPI YAML may only call names present here."""
    REGISTRY[name] = fn


def unregister(name: str) -> None:
    """Remove a hook (used by tests to restore the allowlist)."""
    REGISTRY.pop(name, None)


def run(name: str, *args: Any, **kwargs: Any) -> Any:
    """Execute an allowlisted hook by name, or fail if it was never registered."""
    if name not in REGISTRY:
        raise CatalogError(
            f"Unknown hook {name!r}. Register it in kpi_engine.extensions.hooks.REGISTRY."
        )
    return REGISTRY[name](*args, **kwargs)
