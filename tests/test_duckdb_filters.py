"""DuckDB filter pushdown: IN filters only for columns in the model SELECT.

What this file provides
    Physical and CTE models apply context filters in the extract WHERE when
    the column is in the source SELECT. Cut ignore_filters stay out of DuckDB.
    Dim columns used only inside a CTE are not IN-filtered on the wrapper.

Where it is used
    pytest tests/test_duckdb_filters.py.

When to use
    Add a case when a new filterable column is added to a SQL model SELECT.
"""

import pandas as pd

from kpi_engine import compute, validate
from tests.conftest import find_row, make_context
from tests.test_sql_cte_model import _CTE_SQL, _write_dims, _write_multi_model


def test_physical_model_pushes_select_filters_to_duckdb(parquet_path, config_dir):
    """supplier_name is on the scanned table so DuckDB IN-filters it; region is deferred."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
    )
    result = compute(ctx, config_dir=config_dir)
    sql = " ".join(result["sql"].split())
    assert '"supplier_name" IN (?)' in sql
    assert '"region" IN' not in sql
    assert '"event_month" IN' not in sql
    applied = {row["column"]: row["stage"] for row in result["applied_filters"]}
    assert applied["supplier_name"] == "extract"
    assert applied["region"] == "calc"
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # G ignores region, so worldwide LATE still includes EU: 30 + 15
    assert g["current_value"] == 45.0


def test_cte_select_columns_are_filtered_in_duckdb(parquet_path, extra_config, tmp_path):
    """CTE SELECT includes supplier_name → wrapper WHERE applies the context IN list."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    pd.DataFrame(
        [
            {"supplier_name": "ABC", "active": True},
            {"supplier_name": "XYZ", "active": True},
        ]
    ).to_parquet(suppliers_path, index=False)
    facts = _facts_with_xyz(tmp_path, parquet_path)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _cte_context(
        facts,
        regions_path,
        suppliers_path,
        extra_filters=None,
        supplier=["ABC"],
    )
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert '"supplier_name" IN (?)' in sql
    assert '"eligible" IN' not in sql
    assert '"weight" IN' not in sql

    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # XYZ Mar NA 9999 is dropped by DuckDB; ABC LATE = 30*1 + 15*2 = 60
    assert g["current_value"] == 60.0


def test_cte_filter_omitted_from_output_schema_still_binds(parquet_path, extra_config, tmp_path):
    """A mapped filter does not need to be listed in output_schema to sit on the wrapper."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    schema_without_supplier = [
        {"name": "event_month", "type": "date"},
        {"name": "region", "type": "varchar"},
        {"name": "reason_code", "type": "varchar"},
        {"name": "amount", "type": "decimal"},
    ]
    _write_multi_model(extra_config, _CTE_SQL)
    import yaml

    model_path = extra_config / "models" / "sotif_multi.yaml"
    spec = yaml.safe_load(model_path.read_text())
    spec["output_schema"] = schema_without_supplier
    model_path.write_text(yaml.dump(spec, sort_keys=False), encoding="utf-8")

    ctx = _cte_context(parquet_path, regions_path, suppliers_path, supplier=["ABC"])
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert '"supplier_name" IN (?)' in sql
    assert sql.startswith("SELECT")
    assert "WITH regions AS" in planned["sql"]


def test_mapped_dim_filter_is_applied_on_the_cte_wrapper(parquet_path, extra_config, tmp_path):
    """A mapped filter becomes IN on the wrapper around the whole CTE script."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    cte = _CTE_SQL.replace(
        "  f.supplier_name,\n  f.amount * r.weight AS amount",
        "  f.supplier_name,\n  r.eligible,\n  f.amount * r.weight AS amount",
    )
    _write_multi_model(extra_config, cte)
    ctx = _cte_context(
        parquet_path,
        regions_path,
        suppliers_path,
        extra_filters={"eligible": {"value": [True], "input_text": "simple"}},
        supplier=["ABC"],
    )
    ctx["datasets"]["Regions"]["filter_column_mappings"] = [
        {
            "filter_code": "eligible",
            "column_name": "eligible",
            "operator": "in",
        }
    ]
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert '"eligible" IN (?)' in sql
    assert '"supplier_name" IN (?)' in sql


def test_reason_code_in_select_is_filtered_in_duckdb(parquet_path, extra_config, tmp_path):
    """reason_code is in the CTE SELECT, so a context IN list is applied in DuckDB."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _cte_context(
        parquet_path,
        regions_path,
        suppliers_path,
        extra_filters={"reason_code": {"value": ["LATE_SUPPLIER"], "input_text": "simple"}},
        supplier=["ABC"],
    )
    ctx["datasets"]["Sotif"]["filter_column_mappings"].append(
        {
            "filter_id": 70,
            "filter_code": "reason_code",
            "view_id": 13,
            "column_name": "reason_code",
            "operator": "in",
        }
    )
    planned = validate(ctx, config_dir=extra_config)
    sql = " ".join(planned["sql"].split())
    assert '"reason_code" IN (?)' in sql
    result = compute(ctx, config_dir=extra_config)
    reasons = {r["reason_code"] for r in result["rows"]}
    assert reasons == {"LATE_SUPPLIER"}
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 60.0


def _facts_with_xyz(tmp_path, parquet_path):
    """Copy the fixture facts and add an XYZ row that must be dropped by supplier IN."""
    frame = pd.read_parquet(parquet_path)
    extra = pd.DataFrame(
        [
            {
                "event_month": pd.Timestamp("2026-03-01"),
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "XYZ",
                "amount": 9999,
            }
        ]
    )
    path = tmp_path / "facts_xyz.parquet"
    pd.concat([frame, extra], ignore_index=True).to_parquet(path, index=False)
    return path


def _cte_context(
    parquet_path,
    regions_path,
    suppliers_path,
    *,
    supplier=None,
    extra_filters=None,
):
    """Context for KPI 9020 with three datasets."""
    return make_context(
        parquet_path,
        measures=["current_value"],
        supplier=supplier,
        kpi_id=9020,
        extra_filters=extra_filters,
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
