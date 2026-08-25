"""Host DuckDB session — the only place the engine talks to platform I/O.

What this file provides
    acquire_connection — one DuckDB session for a compute() request.
    register_duckdb_getter — tests / hosts that inject the helper at import time.

Where it is used
    orchestrator.compute. kpi_engine.main does not open DuckDB itself.

Capabilities
    Production uses the connection the platform already manages. The engine
    never authenticates, never opens ADLS, and never closes a host session.
    Local tests fall back to duckdb.connect() when no host helper is present.

When to use
    When copying this repo into the platform, set HOST_DUCKDB_GETTER to the
    existing function (module:function). Do not add credentials here.

    Do not rename this file to platform.py. That shadows the stdlib ``platform``
    module (pandas does ``import platform``) whenever this directory is on
    sys.path.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable

import duckdb

from kpi_engine.exceptions import KPIEngineError
from kpi_engine.runlog import traced

# Copy-time: set this to the platform helper, e.g. "acme.runtime.duckdb:get_connection".
# Runtime override: env KPI_ENGINE_DUCKDB_GETTER=module.path:function_name
HOST_DUCKDB_GETTER = ""

_registered: Callable[[], Any] | None = None


def register_duckdb_getter(fn: Callable[[], Any] | None) -> None:
    """Inject the host helper (or None to clear). Used by tests and host startup."""
    global _registered
    _registered = fn


@traced
def acquire_connection(connection: Any | None = None) -> tuple[Any, bool]:
    """Return (connection, owned). owned=True only for the local test fallback.

    A host session is never closed by the engine.
    """
    if connection is not None:
        return connection, False
    getter = _resolve_getter()
    if getter is not None:
        return getter(), False
    return duckdb.connect(), True


def _resolve_getter() -> Callable[[], Any] | None:
    """Prefer a registered helper, then HOST_DUCKDB_GETTER, else None (local tests)."""
    if _registered is not None:
        return _registered
    spec = (HOST_DUCKDB_GETTER or os.environ.get("KPI_ENGINE_DUCKDB_GETTER") or "").strip()
    if not spec:
        return None
    module_name, sep, attr = spec.partition(":")
    if not sep or not attr:
        raise KPIEngineError(
            f"HOST_DUCKDB_GETTER {spec!r} must be 'module.path:function_name'."
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise KPIEngineError(
            f"Could not import platform DuckDB helper {spec!r}: {exc}"
        ) from exc
    getter = getattr(module, attr, None)
    if getter is None or not callable(getter):
        raise KPIEngineError(f"Platform DuckDB helper {spec!r} is missing or not callable.")
    return getter
