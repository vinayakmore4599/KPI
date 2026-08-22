"""Used join columns appear on the extract SELECT; unused dim columns do not.

What this file provides
    Physical join: regions.eligible is projected when filtered or measured.
    Unused eligible stays off SELECT. SQL wrapper projects used CTE outputs only.

Where it is used
    pytest tests/test_join_select.py.

When to use
    Add a case when a joined context column should (or should not) be retrieved.
"""

from kpi_engine import compute, validate
from tests.conftest import find_row, write_yaml
from tests.test_duckdb_filters import _cte_context
from tests.test_remaining_guards import (
    _join_context,
    _kpi,
    _write_join_model,
    _write_na_only_regions,
)
from tests.test_sql_cte_model import _CTE_SQL, _write_dims, _write_multi_model


def _outer_select(sql: str) -> str:
    """Text of the wrapper SELECT list (before the outer FROM)."""
    head, _sep, _rest = sql.partition("\nFROM ")
    return head


def test_physical_join_selects_used_eligible(parquet_path, extra_config, tmp_path):
    """A filter on eligible projects regions.eligible, not sotif.eligible."""
    regions_path = _write_na_only_regions(tmp_path)
    _write_join_model(extra_config, join_type="inner")
    write_yaml(extra_config / "kpis" / "9870.yaml", _kpi(9870, model="sotif_join"))
    ctx = _join_context(parquet_path, regions_path, kpi_id=9870)
    ctx["filters"]["eligible"] = {"value": [True], "input_text": "simple"}
    ctx["datasets"]["Regions"]["filter_column_mappings"] = [
        {
            "filter_code": "eligible",
            "column_name": "eligible",
            "operator": "in",
        }
    ]
    planned = validate(ctx, config_dir=extra_config)
    sql = planned["sql"]
    assert '"regions"."eligible"' in sql
    assert '"sotif"."eligible"' not in sql
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 30.0


def test_physical_join_omits_unused_eligible(parquet_path, extra_config, tmp_path):
    """eligible is on the dim context list but unused, so it is not selected."""
    regions_path = _write_na_only_regions(tmp_path)
    _write_join_model(extra_config, join_type="inner")
    write_yaml(extra_config / "kpis" / "9871.yaml", _kpi(9871, model="sotif_join"))
    ctx = _join_context(parquet_path, regions_path, kpi_id=9871)
    sql = _outer_select(validate(ctx, config_dir=extra_config)["sql"])
    assert "eligible" not in sql
    assert '"sotif"."region"' in validate(ctx, config_dir=extra_config)["sql"]


def test_sql_wrapper_selects_used_cte_join_column(parquet_path, extra_config, tmp_path):
    """eligible on the CTE output and in a filter is projected on the wrapper."""
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
    sql = _outer_select(validate(ctx, config_dir=extra_config)["sql"])
    assert '"eligible"' in sql
    assert '"weight"' not in sql


def test_sql_wrapper_omits_unused_dim_columns(parquet_path, extra_config, tmp_path):
    """weight/eligible on the dim dataset stay off the wrapper when unused."""
    regions_path, suppliers_path = _write_dims(tmp_path)
    _write_multi_model(extra_config, _CTE_SQL)
    ctx = _cte_context(parquet_path, regions_path, suppliers_path, supplier=["ABC"])
    sql = _outer_select(validate(ctx, config_dir=extra_config)["sql"])
    assert '"eligible"' not in sql
    assert '"weight"' not in sql
    assert '"amount"' in sql
