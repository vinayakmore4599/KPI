"""Shared test fixtures (local parquet only — no ADLS).

What this file provides
    config_dir — path to repo config/kpis and config/models.
    parquet_path — tiny Sotif-like fact table with a deliberate missing month.
    make_context — metadata-shaped JSON pointing at that parquet.

Where it is used
    All tests under tests/ import make_context / fixtures from here.

Capabilities
    NA/LATE_SUPPLIER skips 2025-03 so calendar shift vs row shift can be proven.
    Filters use reporting_month as the selected month (time.filter_code).

When to use
    Extend make_context when a test needs another context field. Keep data
    local; do not read production Delta in unit tests.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine.dates import month_range_inclusive


@pytest.fixture
def config_dir() -> Path:
    """Repo-root config/ (kpis and models YAML)."""
    return Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def parquet_path(tmp_path: Path) -> Path:
    """
    Monthly amounts by reason_code and region.

    NA / LATE_SUPPLIER is missing 2025-03 so a 12-row shift from 2026-03
    would land on the wrong month; calendar shift must still hit 2025-03 (null).
    """
    rows: list[dict] = []
    start = date(2025, 1, 1)
    end = date(2026, 3, 1)
    for month in month_range_inclusive(start, end):
        for region, reason, base in (
            ("NA", "LATE_SUPPLIER", 10),
            ("EU", "LATE_SUPPLIER", 5),
            ("NA", "OTHER", 2),
        ):
            if region == "NA" and reason == "LATE_SUPPLIER" and month == date(2025, 3, 1):
                continue
            rows.append(
                {
                    "event_month": month,
                    "region": region,
                    "reason_code": reason,
                    "supplier_name": "ABC",
                    "amount": base * month.month,
                }
            )
    frame = pd.DataFrame(rows)
    path = tmp_path / "sotif.parquet"
    frame.to_parquet(path, index=False)
    return path


def make_context(
    parquet_path: Path,
    *,
    measures: list[str],
    extra_filters: dict | None = None,
    region: list[str] | None = None,
    supplier: list[str] | None = None,
    month: str = "2026-03",
) -> dict:
    """Build a metadata-shaped context pointing at a local parquet path."""
    filters = {
        "reporting_month": {"value": [month], "input_text": "simple"},
    }
    if region is not None:
        filters["region"] = {"values": region, "input_text": "simple"}
    if supplier is not None:
        filters["Supplier Name"] = {"value": supplier, "input_text": "simple"}
    if extra_filters:
        filters.update(extra_filters)
    return {
        "execution": {
            "source": "metadata",
            "request_id": "REQ-page-001",
            "kpi_id": 3004,
            "view_details": [
                {
                    "view_id": 13,
                    "view_name": "Sotif",
                    "measures_required": [{"measure_key": m} for m in measures],
                }
            ],
            "user_id": "id",
            "business_date": None,
        },
        "filters": filters,
        "datasets": {
            "Sotif": {
                "dataset_id": 21,
                "dataset_name": "CDL_SOTIF",
                "table_type": "PARQUET",
                "path": str(parquet_path),
                "container_name": "command",
                "partition_key": None,
                "alias": "sotif",
                "columns": [
                    "event_month",
                    "region",
                    "reason_code",
                    "supplier_name",
                    "amount",
                ],
                "filter_column_mappings": [
                    {
                        "filter_id": 67,
                        "filter_code": "region",
                        "view_id": 13,
                        "column_name": "region",
                        "operator": "in",
                    },
                    {
                        "filter_id": 68,
                        "filter_code": "Supplier Name",
                        "view_id": 13,
                        "column_name": "supplier_name",
                        "operator": "in",
                    },
                ],
                "join_type": None,
                "join_condition": None,
                "join_managed_by": "udf",
            }
        },
        "udf": {
            "udf_id": 6,
            "udf_name": "sotif",
            "udf_type": "MEASURE",
            "module_path": "udfs.sotif,main",
            "output_type": "df",
        },
        "output": {
            "response_type": "pagination",
            "page": None,
            "page_size": None,
            "limit": None,
        },
    }
