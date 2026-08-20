"""Safe SQL identifier helpers.

What this file provides
    `require_ident` — allow only [A-Za-z_][A-Za-z0-9_]*.
    `quote_ident` — wrap a validated name in double quotes for DuckDB.

Where it is used
    binder (YAML column names), model_sql (SELECT/WHERE/GROUP BY).

Capabilities
    Stops SQL injection from YAML or filter names. Measure `sql:` fields are
    column names, not free-form expressions.

When to use
    Call quote_ident before interpolating any name into SQL. Never concatenate
    user filter values into SQL; those are bound as parameters in model_sql.
"""

from __future__ import annotations

import re

from kpi_engine.exceptions import BindError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_ident(name: str, *, what: str = "identifier") -> str:
    """Accept only simple SQL names (letters, digits, underscore). Reject injection."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise BindError(f"Illegal {what}: {name!r}. Use a simple SQL identifier.")
    return name


def quote_ident(name: str) -> str:
    """Validate then wrap an identifier in double quotes for DuckDB."""
    return f'"{require_ident(name)}"'
