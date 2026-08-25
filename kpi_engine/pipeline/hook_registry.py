"""In-memory hook map. Loader fills it from registries/hooks.yaml.

What this file provides
    REGISTRY, register(name, fn), run(name, ...).

Where it is used
    Loader registers YAML hooks. The hook op calls run().

When to use
    Do not add hook bodies here. Add the function under capabilities/hooks/
    and a row in registries/hooks.yaml.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kpi_engine.exceptions import CatalogError
from kpi_engine.runlog import traced

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
