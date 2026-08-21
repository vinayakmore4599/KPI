"""Platform MEASURE UDF entry: udfs.sotif.main.

What this file provides
    main(context, config_dir=None, connection=None, log_dir=None) → kpi_engine.compute.
    Local test: python udfs/sotif/main.py path/to/context.json

Where it is used
    Metadata calls module_path udfs.sotif.main. This is the only function the
    host should invoke.

Capabilities
    Puts the udfs/ folder on sys.path so kpi_engine and config travel with this
    UDF. kpi_id on the context selects config/kpis/<id>.yaml. DuckDB/ADLS come
    from the platform connection (or HOST_DUCKDB_GETTER).

When to use
    Keep this file a shim. Put calculation changes in YAML or kpi_engine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_UDFS_ROOT = Path(__file__).resolve().parents[1]
if str(_UDFS_ROOT) not in sys.path:
    sys.path.insert(0, str(_UDFS_ROOT))

from kpi_engine import compute  # noqa: E402


def main(
    context: dict[str, Any],
    *,
    config_dir: str | None = None,
    connection: Any | None = None,
    log_dir: str | None = None,
) -> dict[str, Any]:
    """UDF entry the metadata layer calls. Forwards the context to the generic engine."""
    return compute(context, config_dir=config_dir, connection=connection, log_dir=log_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python udfs/sotif/main.py <context.json>")
    context_path = Path(sys.argv[1]).expanduser().resolve()
    context = json.loads(context_path.read_text(encoding="utf-8"))
    log_dir = _UDFS_ROOT.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing engine log under {log_dir}/kpi-compute-<kpi_id>-<timestamp>.log", file=sys.stderr)
    result = main(context, log_dir=str(log_dir))
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))

