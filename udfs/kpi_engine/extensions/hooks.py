"""Allowlisted named hooks for logic the catalog cannot express.

What this file provides
    REGISTRY, register(name, fn), run(name, ...). Compatibility shim.

Where it is used
    Loader registers YAML hooks into REGISTRY. calc_engine.evaluate calls run().
    YAML `hook:` must match a key in registries/hooks.yaml — never a dotted path.

Capabilities
    Hooks receive the densified monthly series plus kpi/plan/spec and return
    a scalar (or a trend (axis, values) tuple).

When to use
    Do not add names here. Add the function under capabilities/hooks/ and a
    row in registries/hooks.yaml. Do not use importlib on context.udf.module_path.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

from kpi_engine.exceptions import CatalogError
from kpi_engine.runlog import traced

warnings.warn(
    "kpi_engine.extensions.hooks is a compatibility shim. "
    "Add hooks under capabilities/hooks/ and registries/hooks.yaml. "
    "This import path will be removed in the next release.",
    DeprecationWarning,
    stacklevel=2,
)

Hook = Callable[..., Any]

REGISTRY: dict[str, Hook] = {}


def register(name: str, fn: Hook) -> None:
    """Register a named hook. KPI YAML may only call names present here."""
    REGISTRY[name] = fn


def unregister(name: str) -> None:
    """Remove a hook (used by tests to restore the allowlist)."""
    REGISTRY.pop(name, None)


@traced
def run(name: str, *args: Any, **kwargs: Any) -> Any:
    """Execute an allowlisted hook by name, or fail if it was never registered."""
    if name not in REGISTRY:
        raise CatalogError(
            f"Unknown hook {name!r}. Register it in registries/hooks.yaml."
        )
    return REGISTRY[name](*args, **kwargs)
