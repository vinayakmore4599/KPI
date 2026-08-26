"""Previous-period at quarter/year grain, Gregorian default.

What this file provides
    offset: { periods: 1 } is one picked-grain step. Gregorian Q1 is Jan–Mar
    and a year starts 1 Jan when calendar: is omitted. fiscal_start_month
    without calendar: fiscal is covered in test_yaml_validation.py.

Where it is used
    pytest tests/test_previous_period_grains.py.

When to use
    Add a case when grain-step offsets or the default calendar change.
"""

from datetime import date

import pandas as pd

from kpi_engine import compute
from tests.conftest import make_context, write_yaml, value_of


def test_gregorian_quarter_previous_period_matches_quarters_offset(tmp_path, extra_config):
    """No calendar: key → Q1 is Jan–Mar; periods: 1 equals quarters: 1."""
    path = tmp_path / "q.parquet"
    rows = []
    for month, amount in (
        ("2025-10-01", 1),
        ("2025-11-01", 2),
        ("2025-12-01", 4),
        ("2026-01-01", 10),
        ("2026-02-01", 20),
        ("2026-03-01", 30),
    ):
        rows.append(
            {
                "event_month": month,
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": amount,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9040.yaml",
        _prev_kpi(9040, grain="quarter", offset={"periods": 1}),
    )
    write_yaml(
        extra_config / "kpis" / "9041.yaml",
        _prev_kpi(9041, grain="quarter", offset={"quarters": 1}),
    )
    ctx_p = make_context(
        path,
        measures=["current_value", "previous_period"],
        supplier=["ABC"],
        kpi_id=9040,
        month="2026-03",
    )
    ctx_q = make_context(
        path,
        measures=["current_value", "previous_period"],
        supplier=["ABC"],
        kpi_id=9041,
        month="2026-03",
    )
    result_p = compute(ctx_p, config_dir=extra_config)
    result_q = compute(ctx_q, config_dir=extra_config)
    assert result_p["parameters"]["anchor"] == "2026-01-01"
    row_p = result_p["rows"][0]
    row_q = result_q["rows"][0]
    assert row_p["current_value"] == {"value": 60.0, "period": "2026-01-01"}
    assert row_p["previous_period"] == {"value": 7.0, "period": "2025-10-01"}
    assert row_q["previous_period"] == row_p["previous_period"]
    assert row_q["current_value"] == row_p["current_value"]


def test_month_offset_at_quarter_grain_truncates(tmp_path, extra_config):
    """offset.months is calendar months then grain truncate, not one quarter step.

    Q1 (1 Jan) minus one month is December, which truncates to Q4. Authors who
    want the previous quarter must use offset: { periods: 1 } or { quarters: 1 }.
    """
    path = tmp_path / "qm.parquet"
    pd.DataFrame(
        [
            {
                "event_month": month,
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": amount,
            }
            for month, amount in (
                ("2025-10-01", 7),
                ("2026-01-01", 10),
                ("2026-02-01", 20),
                ("2026-03-01", 30),
            )
        ]
    ).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9042.yaml",
        _prev_kpi(9042, grain="quarter", offset={"months": 1}),
    )
    ctx = make_context(
        path,
        measures=["current_value", "previous_period"],
        supplier=["ABC"],
        kpi_id=9042,
        month="2026-03",
    )
    row = compute(ctx, config_dir=extra_config)["rows"][0]
    assert row["current_value"]["period"] == "2026-01-01"
    assert row["previous_period"]["period"] == "2025-10-01"
    assert value_of(row["previous_period"], "value") == 7.0


def test_gregorian_year_previous_period(tmp_path, extra_config):
    """No calendar: key → year starts 1 Jan; periods: 1 is the prior calendar year."""
    path = tmp_path / "y.parquet"
    rows = []
    for year, amount in ((2025, 5), (2026, 11)):
        for month in range(1, 13):
            rows.append(
                {
                    "event_month": date(year, month, 1),
                    "region": "NA",
                    "reason_code": "LATE_SUPPLIER",
                    "supplier_name": "ABC",
                    "amount": amount,
                }
            )
    pd.DataFrame(rows).to_parquet(path, index=False)
    write_yaml(
        extra_config / "kpis" / "9043.yaml",
        _prev_kpi(9043, grain="year", offset={"periods": 1}),
    )
    write_yaml(
        extra_config / "kpis" / "9044.yaml",
        _prev_kpi(9044, grain="year", offset={"years": 1}),
    )
    ctx_p = make_context(
        path,
        measures=["current_value", "previous_period"],
        supplier=["ABC"],
        kpi_id=9043,
        month="2026-03",
    )
    ctx_y = make_context(
        path,
        measures=["current_value", "previous_period"],
        supplier=["ABC"],
        kpi_id=9044,
        month="2026-03",
    )
    result_p = compute(ctx_p, config_dir=extra_config)
    result_y = compute(ctx_y, config_dir=extra_config)
    assert result_p["parameters"]["anchor"] == "2026-01-01"
    row_p = result_p["rows"][0]
    row_y = result_y["rows"][0]
    assert row_p["current_value"] == {"value": 132.0, "period": "2026-01-01"}
    assert row_p["previous_period"] == {"value": 60.0, "period": "2025-01-01"}
    assert row_y["previous_period"] == row_p["previous_period"]


def _prev_kpi(kpi_id: int, *, grain: str, offset: dict) -> dict:
    """Single global-cut KPI; calendar omitted so gregorian is the default."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": grain,
            "filter_code": "reporting_month",
        },
        "dimensions": [{"name": "reason_code", "kind": "dimension"}],
        "default_dimensions": [],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "previous_period": {"of": "sotif_value", "op": "point", "offset": offset},
        },
    }
