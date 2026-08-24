"""End-to-end compute covering SQL CTEs, time.format, name fold, constant, and rank.

What this file provides
    One request: multi-CTE SQL model, mmyyyy time column, Region / Reason_Code
    filters, op: constant, op: rank, and the UDF entry.

Where it is used
    pytest tests/test_plan_integration.py.

When to use
    Keep this as the combined regression for the SQL-filter / format / rank slice.
"""

from __future__ import annotations

import pandas as pd

from kpi_engine import compute, validate
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml
from tests.test_sql_cte_model import _CTE_SQL, _write_dims
from kpi_engine.main import main


def test_sql_cte_format_names_constant_rank_compute(parquet_path, extra_config, tmp_path):
    """Wrapper IN + mmyyyy + Region/Reason_Code + constant/rank in one compute()."""
    facts = _mmyyyy_facts(parquet_path, tmp_path)
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_sql_model(extra_config, _CTE_SQL)
    write_yaml(extra_config / "kpis" / "9850.yaml", _integrated_kpi())

    ctx = _integrated_context(facts, regions_path, suppliers_path)
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert "WITH regions AS" in planned["sql"]
    assert sql.startswith("SELECT")
    assert "strptime" in planned["sql"]
    assert "%m%Y" in planned["sql"]
    assert '"supplier_name" IN (?)' in sql
    assert '"reason_code" IN (?, ?)' in sql
    assert '"region" IN' not in sql
    assert '"event_month" IN' not in sql

    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    r_late = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    r_other = find_row(result, cut="R", reason="OTHER", region="NA")

    # CTE multiplies amount by region weight (NA 1, EU 2). G ignores Region=NA.
    assert g_late["current_value"] == 60.0
    assert g_other["current_value"] == 6.0
    assert r_late["current_value"] == 30.0
    assert r_other["current_value"] == 6.0
    assert {r["region"] for r in result["rows"] if r["output_cut"] == "R"} == {"NA"}

    assert g_late["target"] == 0.98
    assert r_late["target"] == 0.98
    assert abs(g_late["percent_gt"] - (60.0 / 0.98) * 100) < 1e-9

    assert g_late["reason_code_rank"] == 1
    assert g_other["reason_code_rank"] == 2
    assert all("reason_code_rank" not in r for r in result["rows"] if r["output_cut"] == "R")

    udf = main(ctx, config_dir=str(extra_config))
    assert udf["rows"] == result["rows"]


def _mmyyyy_facts(parquet_path, tmp_path):
    """Rewrite event_month to mmyyyy strings (032026) for the SQL-model scan."""
    frame = pd.read_parquet(parquet_path)
    stamp = pd.to_datetime(frame["event_month"])
    frame["event_month"] = stamp.dt.strftime("%m%Y")
    path = tmp_path / "facts_mmyyyy.parquet"
    frame.to_parquet(path, index=False)
    return path


def _write_sql_model(extra_config, sql: str) -> None:
    """kind: sql model with three path tokens; KPI YAML is written separately."""
    write_yaml(
        extra_config / "models" / "sotif_multi.yaml",
        {
            "model_id": "sotif_multi",
            "kind": "sql",
            "required_aliases": ["sotif", "regions", "suppliers"],
            "output_schema": [
                {"name": "event_month", "type": "varchar"},
                {"name": "region", "type": "varchar"},
                {"name": "reason_code", "type": "varchar"},
                {"name": "supplier_name", "type": "varchar"},
                {"name": "amount", "type": "decimal"},
            ],
            "sql": sql.strip("\n") + "\n",
        },
    )


def _integrated_kpi() -> dict:
    """KPI that uses every new contract: format, constant, rank, name-folded cuts."""
    spec = minimal_kpi(9850, model="sotif_multi")
    spec["time"]["format"] = "mmyyyy"
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "target": {"op": "constant", "value": 0.98},
        "percent_gt": {
            "op": "arithmetic",
            "fn": "percent",
            "left": "current_value",
            "right": "target",
        },
        "reason_code_rank": {
            "op": "rank",
            "of": "current_value",
            "group_by": ["Reason_Code"],
            "order": "desc",
            "cuts": ["G"],
        },
    }
    return spec


def _integrated_context(facts, regions_path, suppliers_path) -> dict:
    """Context with Region / Reason_Code spellings and three SQL-model datasets."""
    ctx = make_context(
        facts,
        measures=["current_value", "target", "percent_gt", "reason_code_rank"],
        supplier=["ABC"],
        kpi_id=9850,
        month="032026",
        extra_datasets={
            "Regions": {
                "dataset_id": 22,
                "dataset_name": "REGIONS",
                "table_type": "PARQUET",
                "path": str(regions_path),
                "alias": "regions",
                "columns": ["region", "eligible", "weight"],
                "filter_column_mappings": [],
            },
            "Suppliers": {
                "dataset_id": 23,
                "dataset_name": "SUPPLIERS",
                "table_type": "PARQUET",
                "path": str(suppliers_path),
                "alias": "suppliers",
                "columns": ["supplier_name", "active"],
                "filter_column_mappings": [],
            },
        },
    )
    ctx["filters"]["Region"] = {"values": ["NA"], "input_text": "simple"}
    ctx["filters"]["Reason_Code"] = {"values": ["LATE_SUPPLIER", "OTHER"], "input_text": "simple"}
    return ctx
