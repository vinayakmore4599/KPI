"""Multi-source SQL model tests: CTE joins several datasets.

What this file provides
    kind: sql WITH ... queries that join facts + dimension parquets via
    $alias_path tokens. Path parameters follow CTE appearance order, not
    required_aliases order. Time range still wraps the subquery.

Where it is used
    pytest tests/test_sql_cte_model.py.

When to use
    Add a case when a KPI onboards with a CTE model instead of physical joins.
"""

import pandas as pd

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, write_yaml

_CTE_SQL = """
WITH regions AS (
  SELECT region, eligible, weight
  FROM read_parquet($regions_path)
),
suppliers AS (
  SELECT supplier_name, active
  FROM read_parquet($suppliers_path)
),
facts AS (
  SELECT event_month, region, reason_code, supplier_name, amount
  FROM read_parquet($sotif_path)
)
SELECT
  f.event_month,
  f.region,
  f.reason_code,
  f.supplier_name,
  f.amount * r.weight AS amount
FROM facts f
INNER JOIN regions r ON f.region = r.region
INNER JOIN suppliers s ON f.supplier_name = s.supplier_name
WHERE r.eligible = TRUE
  AND s.active = TRUE
"""


def test_cte_joins_facts_and_dims(parquet_path, extra_config, tmp_path):
    """CTE multiplies fact amount by region weight and drops ineligible / inactive rows."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _multi_context(
        parquet_path,
        regions_path,
        suppliers_path,
        measures=["current_value", "value_3m"],
    )
    planned = validate(ctx, config_dir=extra_config)
    sql = planned["sql"]
    assert "WITH regions AS" in sql
    assert sql.count("read_parquet(?)") == 3
    assert '"event_month" IN' not in " ".join(sql.split())
    assert "date_trunc('month'" in sql
    assert ">=" in sql

    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # Mar: NA 30 * 1 + EU 15 * 2 = 60
    assert g["current_value"] == 60.0
    # Jan–Mar: NA 60 * 1 + EU 30 * 2 = 120
    assert g["value_3m"] == 120.0

    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    assert na["current_value"] == 30.0
    assert eu["current_value"] == 30.0
    r_regions = {
        r["region"]
        for r in result["rows"]
        if r["output_cut"] == "R" and r["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA", "EU"}


def test_cte_path_params_follow_sql_order(parquet_path, extra_config, tmp_path):
    """$alias_path is bound in appearance order even when required_aliases lists facts first."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _multi_context(
        parquet_path,
        regions_path,
        suppliers_path,
        measures=["current_value"],
    )
    planned = validate(ctx, config_dir=extra_config)
    # FROM-clause params come first: regions, suppliers, sotif (CTE order),
    # then time-range start/end, then supplier IN.
    assert planned["param_count"] >= 5
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 60.0


def test_cte_drops_ineligible_region(parquet_path, extra_config, tmp_path):
    """A region with eligible=false is not in the CTE output, so it cannot appear on R."""
    regions_path, suppliers_path = _write_dims(tmp_path, eu_eligible=False)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _multi_context(
        parquet_path,
        regions_path,
        suppliers_path,
        measures=["current_value", "value_3m"],
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 30.0
    assert g["value_3m"] == 60.0
    r_regions = {
        r["region"]
        for r in result["rows"]
        if r["output_cut"] == "R" and r["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}


def test_cte_missing_required_alias(parquet_path, extra_config):
    """Without a context path or a model default, a required alias is a hard error."""
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9020,
    )
    try:
        validate(ctx, config_dir=extra_config)
    except BindError as exc:
        assert "regions" in str(exc)
        assert "default_path" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_cte_default_paths_when_context_omits_dims(parquet_path, extra_config, tmp_path):
    """Dim tables can live only in model default_paths; facts still come from context."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(
        extra_config,
        _CTE_SQL,
        default_paths={"regions": str(regions_path), "suppliers": str(suppliers_path)},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9020,
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 60.0
    assert g["value_3m"] == 120.0


def test_cte_context_path_overrides_model_default(parquet_path, extra_config, tmp_path):
    """When context sends a path, it wins over YAML default_paths."""
    default_regions, suppliers_path = _write_dims(tmp_path, eu_eligible=True, stem="default")
    context_regions, _ = _write_dims(tmp_path, eu_eligible=False, stem="context")
    _write_multi_model(
        extra_config,
        _CTE_SQL,
        default_paths={
            "regions": str(default_regions),
            "suppliers": str(suppliers_path),
        },
    )
    ctx = _multi_context(
        parquet_path,
        context_regions,
        suppliers_path,
        measures=["current_value"],
    )
    result = compute(ctx, config_dir=extra_config)
    # Context EU is ineligible; default file would have kept EU (current 60).
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 30.0


def test_cte_empty_context_path_uses_default(parquet_path, extra_config, tmp_path):
    """A dataset entry with a blank path still picks up the model default."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(
        extra_config,
        _CTE_SQL,
        default_paths={"regions": str(regions_path), "suppliers": str(suppliers_path)},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9020,
        extra_datasets={
            "Regions": {
                "dataset_id": 22,
                "dataset_name": "REGIONS",
                "table_type": "PARQUET",
                "path": None,
                "alias": "regions",
                "columns": ["region", "eligible", "weight"],
                "filter_column_mappings": [],
            }
        },
    )
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 60.0


def test_cte_sources_default_path(parquet_path, extra_config, tmp_path):
    """sources.<alias>.default_path is the same fallback as the default_paths map."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(
        extra_config,
        _CTE_SQL,
        sources={
            "sotif": {"alias": "sotif"},
            "regions": {
                "alias": "regions",
                "default_path": str(regions_path),
                "table_type": "PARQUET",
            },
            "suppliers": {
                "alias": "suppliers",
                "default_path": str(suppliers_path),
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9020,
    )
    result = compute(ctx, config_dir=extra_config)
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] == 60.0


def test_cte_unknown_path_token(parquet_path, extra_config, tmp_path):
    """$alias_path that is not a bound alias fails at compile, not inside DuckDB."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(extra_config, _CTE_SQL.replace("$sotif_path", "$missing_path"))
    ctx = _multi_context(
        parquet_path,
        regions_path,
        suppliers_path,
        measures=["current_value"],
    )
    try:
        validate(ctx, config_dir=extra_config)
    except BindError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected BindError")


def _write_dims(tmp_path, *, eu_eligible: bool = True, stem: str = "") -> tuple:
    """Region weight/eligibility and supplier active flags as parquet files."""
    prefix = f"{stem}_" if stem else ""
    regions = tmp_path / f"{prefix}regions.parquet"
    suppliers = tmp_path / f"{prefix}suppliers.parquet"
    pd.DataFrame(
        [
            {"region": "NA", "eligible": True, "weight": 1.0},
            {"region": "EU", "eligible": eu_eligible, "weight": 2.0},
            {"region": "APAC", "eligible": True, "weight": 9.0},
        ]
    ).to_parquet(regions, index=False)
    pd.DataFrame(
        [
            {"supplier_name": "ABC", "active": True},
            {"supplier_name": "XYZ", "active": False},
        ]
    ).to_parquet(suppliers, index=False)
    return regions, suppliers


def _write_multi_model(
    extra_config,
    sql: str,
    default_paths: dict | None = None,
    sources: dict | None = None,
) -> None:
    """KPI 9020 + kind: sql model that joins three datasets in a CTE."""
    payload: dict = {
        "model_id": "sotif_multi",
        "kind": "sql",
        "required_aliases": ["sotif", "regions", "suppliers"],
        "output_schema": [
            {"name": "event_month", "type": "date"},
            {"name": "region", "type": "varchar"},
            {"name": "reason_code", "type": "varchar"},
            {"name": "supplier_name", "type": "varchar"},
            {"name": "amount", "type": "decimal"},
        ],
        "sql": sql.strip("\n") + "\n",
    }
    if default_paths:
        payload["default_paths"] = default_paths
    if sources:
        payload["sources"] = sources
    write_yaml(extra_config / "models" / "sotif_multi.yaml", payload)
    write_yaml(extra_config / "kpis" / "9020.yaml", _kpi_9020())


def _multi_context(parquet_path, regions_path, suppliers_path, *, measures: list[str]) -> dict:
    """Context with three datasets bound by alias to the CTE model."""
    return make_context(
        parquet_path,
        measures=measures,
        supplier=["ABC"],
        kpi_id=9020,
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


def _kpi_9020() -> dict:
    """Same cuts/measures as 3004, pointed at the CTE model."""
    return {
        "kpi_id": 9020,
        "version": 1,
        "model": "sotif_multi",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "default_dimensions": ["reason_code"],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [
            {
                "name": "G",
                "group_by": [],
                "exclude_from_grain": ["region"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
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
