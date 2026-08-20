"""Safe SQL identifiers. YAML may only name columns; never interpolate raw SQL."""

from __future__ import annotations

import re

from kpi_engine.exceptions import BindError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_ident(name: str, *, what: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise BindError(f"Illegal {what}: {name!r}. Use a simple SQL identifier.")
    return name


def quote_ident(name: str) -> str:
    return f'"{require_ident(name)}"'
