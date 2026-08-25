"""Shared test fixtures (local parquet only — no ADLS).

What this file provides
    config_dir — path to repo config/kpis and config/models (optional kpi_group folders).
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

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from kpi_engine.contracts import CutSpec
from kpi_engine.dates import month_range_inclusive


@pytest.fixture(autouse=True)
def _redirect_kpi_logs(tmp_path_factory, monkeypatch):
    """Keep per-run log files out of the repo during tests."""
    root = tmp_path_factory.getbasetemp() / "kpi_engine_logs"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("KPI_ENGINE_LOG_DIR", str(root))


@pytest.fixture
def config_dir() -> Path:
    """Repo udfs/config (kpis and models YAML)."""
    return Path(__file__).resolve().parents[1] / "udfs" / "config"


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


@pytest.fixture
def extra_config(tmp_path: Path, config_dir: Path) -> Path:
    """Writable copy of repo config for test-only KPI / model YAML."""
    dest = tmp_path / "config"
    shutil.copytree(config_dir, dest)
    return dest



def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write a KPI or model YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


def sotif_cuts(*, also_emit: bool = True, ignore_region: bool = True) -> list[dict[str, Any]]:
    """Extras-only G/R cuts used with default_dimensions: [reason_code]."""
    g: dict[str, Any] = {"name": "G", "group_by": []}
    if ignore_region:
        g["exclude_from_grain"] = ["region"]
        g["ignore_filters"] = ["region"]
    else:
        g["ignore_filters"] = []
    cuts = [g]
    if also_emit:
        g["also_emit"] = ["R"]
        cuts.append({"name": "R", "group_by": ["region"], "ignore_filters": []})
    return cuts


def cut_spec(
    name: str,
    *,
    extras: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    ignore: tuple[str, ...] = (),
    also_emit: tuple[str, ...] = (),
) -> CutSpec:
    """CutSpec with extras-only group_by (pair with KpiSpec.request_grain)."""
    return CutSpec(
        name=name,
        group_by=extras,
        ignore_filters=ignore,
        also_emit=also_emit,
        exclude_from_grain=exclude,
    )


def minimal_kpi(kpi_id: int, **overrides: Any) -> dict:
    """Sotif-shaped KPI YAML dict for test-only KPIs. Any top-level key can be overridden."""
    spec: dict[str, Any] = {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "from": "reason_code"},
            {"name": "region", "from": "region"},
            {"name": "supplier", "from": "supplier_name"},
        ],
        "default_dimensions": ["reason_code"],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": sotif_cuts(),
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    }
    spec.update(overrides)
    return spec


def find_row(
    result: dict,
    *,
    cut: str,
    reason: str | None = None,
    region: str | None = None,
) -> dict:
    """Return the unique result row for a cut / reason / region combo."""
    matches = []
    for row in result["rows"]:
        if row.get("output_cut") != cut:
            continue
        if reason is not None and row.get("reason_code") != reason:
            continue
        if region is not None and row.get("region") != region:
            continue
        matches.append(row)
    assert matches, result["rows"]
    assert len(matches) == 1, matches
    return matches[0]


def make_context(
    parquet_path: Path,
    *,
    measures: list[str],
    extra_filters: dict | None = None,
    region: list[str] | None = None,
    supplier: list[str] | None = None,
    month: str = "2026-03",
    kpi_id: int = 3004,
    page: int | None = None,
    page_size: int | None = None,
    limit: int | None = None,
    business_date: str | None = "2099-01-01",
    extra_datasets: dict | None = None,
    time_grain: str | None = None,
    parameters: dict | None = None,
    selected_dimensions: Any = None,
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
    datasets = {
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
    }
    if extra_datasets:
        datasets.update(extra_datasets)
    ctx = {
        "execution": {
            "source": "metadata",
            "request_id": "REQ-page-001",
            "kpi_id": kpi_id,
            "view_details": [
                {
                    "view_id": 13,
                    "view_name": "Sotif",
                    "measures_required": [{"measure_key": m} for m in measures],
                }
            ],
            "user_id": "id",
            "business_date": business_date,
        },
        "filters": filters,
        "datasets": datasets,
        "udf": {
            "udf_id": 6,
            "udf_name": "kpi_engine",
            "udf_type": "MEASURE",
            "module_path": "udfs.kpi_engine.main",
            "output_type": "df",
        },
        "output": {
            "response_type": "pagination",
            "page": page,
            "page_size": page_size,
            "limit": limit,
        },
    }
    params = dict(parameters or {})
    if time_grain is not None:
        params["time_grain"] = time_grain
    if params:
        ctx["parameters"] = params
    if selected_dimensions is not None:
        ctx["selected_dimensions"] = selected_dimensions
    return ctx
