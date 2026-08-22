"""KPI time.format: context parse and DuckDB bucket for non-ISO stored periods.

What this file provides
    mmyyyy (062026) on the time filter and on a parquet column, plus ISO default.

Where it is used
    pytest tests/test_time_format.py.

When to use
    Add a case when a new time.format alias is added.
"""

from datetime import date

import pandas as pd

from kpi_engine import compute, validate
from kpi_engine.core.binder import load_kpi
from kpi_engine.dates import parse_date
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_parse_date_mmyyyy():
    """062026 and 62026 both become June 2026."""
    assert parse_date("062026", fmt="mmyyyy") == date(2026, 6, 1)
    assert parse_date("62026", fmt="mmyyyy") == date(2026, 6, 1)
    assert parse_date("2026-03", fmt="yyyy-mm") == date(2026, 3, 1)


def test_mmyyyy_column_and_filter(tmp_path, extra_config):
    """Stored event_month 032026 is bucketed and filtered as March 2026."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "032026",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": 30,
            },
            {
                "event_month": "022026",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": 20,
            },
        ]
    )
    path = tmp_path / "mmyyyy.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(9830)
    spec["time"]["format"] = "mmyyyy"
    write_yaml(extra_config / "kpis" / "9830.yaml", spec)
    kpi = load_kpi(9830, extra_config)
    assert kpi.time is not None and kpi.time.format == "mmyyyy"
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9830, month="032026")
    planned = validate(ctx, config_dir=extra_config)
    assert "strptime" in planned["sql"]
    assert "%m%Y" in planned["sql"]
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["current_value"] == 30.0
