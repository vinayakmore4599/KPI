"""Allowlisted named hooks. YAML may only reference names in REGISTRY."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from kpi_engine.exceptions import CatalogError

Hook = Callable[..., pd.DataFrame]

REGISTRY: dict[str, Hook] = {}


def register(name: str, fn: Hook) -> None:
    REGISTRY[name] = fn


def run(name: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
    if name not in REGISTRY:
        raise CatalogError(
            f"Unknown hook {name!r}. Register it in kpi_engine.extensions.hooks.REGISTRY."
        )
    return REGISTRY[name](*args, **kwargs)
