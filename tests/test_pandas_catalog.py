"""Pandas catalog on the DuckDB extract: column ops, where, maps, windows.

What this file provides
    columns + op: mul (SUM of products), optional agg, filtered agg, dimension
    map, cumulative vs trailing, last on a balance, n-ary arithmetic, 3004.

Where it is used
    pytest tests/test_pandas_catalog.py.

When to use
    Add a case when a new catalog op lands in capabilities/.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.contracts import OutputSpec, TimeSpec
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.time_planner import lookback_for
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _product_frame(tmp_path, rows=None) -> str:
    """Two-column fact table for mul tests."""
    frame = pd.DataFrame(
        rows
        or [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 1,
                "fullqty": 10,
            },
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 0,
                "fullqty": 4,
            },
        ]
    )
    path = tmp_path / "product.parquet"
    frame.to_parquet(path, index=False)
    return path


def _mul_kpi(kpi_id: int, *, with_agg: bool) -> dict:
    spec = minimal_kpi(
        kpi_id,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    fact = {"columns": ["ontime", "fullqty"], "op": "mul"}
    if with_agg:
        fact["agg"] = "sum"
    spec["base_measures"] = {"sotif_value": fact}
    return spec


def _product_context(path, extra_config, kpi_id: int):
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=kpi_id)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "ontime",
        "fullqty",
    ]
    return ctx


def test_mul_two_columns_is_sum_of_products(tmp_path, extra_config):
    """(1,10) and (0,4) with op: mul + agg: sum → 10, not 14."""
    path = _product_frame(tmp_path)
    write_yaml(extra_config / "kpis" / "9801.yaml", _mul_kpi(9801, with_agg=True))
    load_kpi(9801, extra_config)
    row = find_row(
        compute(_product_context(path, extra_config, 9801), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 10.0


def test_pandas_sotif_value_is_reused_by_windows_and_yoy(tmp_path, extra_config):
    """One base fact (mul) feeds point, trailing window, and arithmetic — same as 3004."""
    path = _product_frame(
        tmp_path,
        [
            {
                "event_month": "2026-01-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 1,
                "fullqty": 10,
            },
            {
                "event_month": "2026-02-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 2,
                "fullqty": 5,
            },
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 1,
                "fullqty": 10,
            },
            {
                "event_month": "2025-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 1,
                "fullqty": 4,
            },
        ],
    )
    spec = _mul_kpi(9810, with_agg=True)
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "previous_year_value": {"of": "sotif_value", "op": "point", "offset": {"years": 1}},
        "value_3m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 3},
            "inclusive": True,
        },
        "yoy_month": {
            "op": "arithmetic",
            "fn": "growth_pct",
            "left": "current_value",
            "right": "previous_year_value",
        },
    }
    write_yaml(extra_config / "kpis" / "9810.yaml", spec)
    ctx = _product_context(path, extra_config, 9810)
    ctx["execution"]["view_details"][0]["measures_required"] = [
        {"measure_key": k}
        for k in ("current_value", "previous_year_value", "value_3m", "yoy_month")
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 10.0
    assert value_of(row, "previous_year_value") == 4.0
    assert value_of(row, "value_3m") == 30.0  # Jan 10 + Feb 10 + Mar 10
    assert row["yoy_month"] == pytest.approx((10 - 4) / 4)


def test_mul_without_agg_still_products_then_sum(tmp_path, extra_config):
    """agg is optional; two rows still fold with the default sum."""
    path = _product_frame(tmp_path)
    write_yaml(extra_config / "kpis" / "9802.yaml", _mul_kpi(9802, with_agg=False))
    row = find_row(
        compute(_product_context(path, extra_config, 9802), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 10.0


def test_mul_single_row_is_the_product(tmp_path, extra_config):
    """One retrieved row 2 × 5 = 10."""
    path = _product_frame(
        tmp_path,
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "ontime": 2,
                "fullqty": 5,
            }
        ],
    )
    write_yaml(extra_config / "kpis" / "9803.yaml", _mul_kpi(9803, with_agg=False))
    row = find_row(
        compute(_product_context(path, extra_config, 9803), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 10.0


def test_filtered_agg_where_status_in(tmp_path, extra_config):
    """where status in [O] sums only matching rows."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "status": "O",
                "amount": 30,
            },
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "status": "F",
                "amount": 9,
            },
        ]
    )
    path = tmp_path / "status.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9804,
        measures={
            "current_value": {"of": "open_amount", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "open_amount": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "status", "op": "in", "values": ["O"]},
        }
    }
    write_yaml(extra_config / "kpis" / "9804.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9804)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "status",
        "amount",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 30.0


def _where_frame(tmp_path) -> str:
    """Mixed status / amount / object-typed amount for where: tests."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "account_id": "A1",
                "status": "O",
                "amount": "30",
            },
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "account_id": "A2",
                "status": "F",
                "amount": "9",
            },
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "account_id": "A3",
                "status": None,
                "amount": "0",
            },
        ]
    )
    path = tmp_path / "where.parquet"
    frame.to_parquet(path, index=False)
    return path


def _where_columns() -> list[str]:
    return [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "account_id",
        "status",
        "amount",
    ]


def test_where_numeric_ops_and_count_distinct(tmp_path, extra_config):
    """gt/lt/between on object-dtype amount; active accounts with amount > 0."""
    path = _where_frame(tmp_path)
    spec = minimal_kpi(
        9806,
        measures={
            "above_ten": {"of": "gt_amt", "op": "point", "offset": {"months": 0}},
            "below_ten": {"of": "lt_amt", "op": "point", "offset": {"months": 0}},
            "mid_band": {"of": "between_amt", "op": "point", "offset": {"months": 0}},
            "active_accounts": {"of": "live_ids", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "gt_amt": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "amount", "op": "gt", "value": 10},
        },
        "lt_amt": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "amount", "op": "lt", "value": 10},
        },
        "between_amt": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "amount", "op": "between", "values": [5, 40]},
        },
        "live_ids": {
            "sql": "account_id",
            "agg": "count_distinct",
            "where": {"column": "amount", "op": "gt", "value": 0},
        },
    }
    write_yaml(extra_config / "kpis" / "9806.yaml", spec)
    ctx = make_context(
        path,
        measures=["above_ten", "below_ten", "mid_band", "active_accounts"],
        supplier=["ABC"],
        kpi_id=9806,
    )
    ctx["datasets"]["Sotif"]["columns"] = _where_columns()
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["above_ten"] == pytest.approx(30.0)
    assert row["below_ten"] == pytest.approx(9.0)
    assert row["mid_band"] == pytest.approx(39.0)
    assert row["active_accounts"] == pytest.approx(2.0)


def test_where_ne_excludes_null_status(tmp_path, extra_config):
    """SQL-style ne: null status does not pass (F13)."""
    path = _where_frame(tmp_path)
    spec = minimal_kpi(
        9807,
        measures={
            "not_open": {"of": "ne_amt", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "ne_amt": {
            "sql": "amount",
            "agg": "count",
            "where": {"column": "status", "op": "ne", "value": "O"},
        }
    }
    write_yaml(extra_config / "kpis" / "9807.yaml", spec)
    ctx = make_context(path, measures=["not_open"], supplier=["ABC"], kpi_id=9807)
    ctx["datasets"]["Sotif"]["columns"] = _where_columns()
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    # Only status F matches; null is excluded.
    assert row["not_open"] == pytest.approx(1.0)


def test_dimension_map_rewrites_extract_codes(tmp_path, extra_config):
    """Extract O becomes JSON Open via dimensions.map."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "status": "O",
                "amount": 10,
            }
        ]
    )
    path = tmp_path / "map.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(9805)
    spec["dimensions"] = [
        {"name": "reason_code"},
        {"name": "region"},
        {
            "name": "order_status",
            "from": "status",
            "map": {"O": "Open", "P": "Processing"},
            "default": "Other",
        },
    ]
    spec["default_dimensions"] = ["reason_code", "region", "order_status"]
    spec["cuts"] = [
        {
            "name": "S",
            "group_by": [],
            "ignore_filters": [],
        }
    ]
    spec["default_cut"] = "S"
    write_yaml(extra_config / "kpis" / "9805.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9805)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "status",
        "amount",
    ]
    rows = compute(ctx, config_dir=extra_config)["rows"]
    assert any(r.get("order_status") == "Open" for r in rows), rows


def test_cumulative_window_is_ytd_not_trailing_3(parquet_path, extra_config):
    """December YTD includes Jan–Dec; trailing 3 is only Oct–Dec."""
    spec = minimal_kpi(
        9806,
        measures={
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "value_ytd": {"of": "sotif_value", "op": "window", "range": "cumulative"},
        },
    )
    write_yaml(extra_config / "kpis" / "9806.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["value_3m", "value_ytd"],
        supplier=["ABC"],
        kpi_id=9806,
        month="2025-12",
    )
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    # Oct+Nov+Dec = 10*(10+11+12) = 330. YTD missing March: 10*(1+…+12) - 30 = 750.
    assert value_of(row, "value_3m") == 330.0
    assert value_of(row, "value_ytd") == 750.0


def test_last_on_balance_does_not_sum_days(tmp_path, extra_config):
    """Two snapshot rows in the month: last is 120, not 220."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-10",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "balance": 100,
            },
            {
                "event_month": "2026-03-20",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "balance": 120,
            },
        ]
    )
    path = tmp_path / "balance.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9807,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {"sotif_value": {"sql": "balance", "agg": "last"}}
    write_yaml(extra_config / "kpis" / "9807.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9807)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "balance",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 120.0


def test_arithmetic_operand_list_subtracts(tmp_path, extra_config):
    """fn: sub of: [gross, opex] is MEASURE-style composition."""
    frame = pd.DataFrame(
        [
            {
                "event_month": "2026-03-01",
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "revenue": 10,
                "cost": 4,
            }
        ]
    )
    path = tmp_path / "arith.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        9808,
        measures={
            "gross_value": {"of": "gross", "op": "point", "offset": {"months": 0}},
            "opex_value": {"of": "opex", "op": "point", "offset": {"months": 0}},
            "net_value": {"op": "arithmetic", "fn": "sub", "of": ["gross_value", "opex_value"]},
        },
    )
    spec["base_measures"] = {
        "gross": {"sql": "revenue", "agg": "sum"},
        "opex": {"sql": "cost", "agg": "sum"},
    }
    write_yaml(extra_config / "kpis" / "9808.yaml", spec)
    ctx = make_context(path, measures=["net_value"], supplier=["ABC"], kpi_id=9808)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "revenue",
        "cost",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "net_value") == 6.0


def test_3004_sql_amount_still_parses_and_computes(parquet_path, config_dir):
    """KPI 3004 keeps sql: amount + agg: sum; current / 3m / YoY still work."""
    kpi = load_kpi(3004, config_dir)
    assert kpi.base_measures[0].sql == "amount"
    assert kpi.base_measures[0].agg == "sum"
    assert kpi.base_measures[0].row_op is None
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m", "previous_year_value"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(row, "current_value") == 30.0
    assert value_of(row, "value_3m") == 60.0
    assert row["previous_year_value"] is None


def test_cumulative_lookback_reaches_year_start():
    """A YTD window in June needs five periods of history."""
    monthly = TimeSpec(column="event_month", grain="month", filter_code="reporting_month")
    spec = OutputSpec(key="ytd", kind="window", of="v", window_range="cumulative")
    assert lookback_for(spec, {}, monthly, anchor=date(2026, 6, 1)) == 5


def test_binder_parses_columns_op_and_range(extra_config):
    """columns/op, where, range, and of: lists survive load_kpi."""
    spec = _mul_kpi(9809, with_agg=False)
    spec["measures"]["value_ytd"] = {
        "of": "sotif_value",
        "op": "window",
        "range": "cumulative",
    }
    spec["measures"]["combo"] = {
        "op": "arithmetic",
        "fn": "add",
        "of": ["current_value", "value_ytd"],
    }
    write_yaml(extra_config / "kpis" / "9809.yaml", spec)
    kpi = load_kpi(9809, extra_config)
    base = kpi.base_measures[0]
    assert base.columns == ("ontime", "fullqty")
    assert base.row_op == "mul"
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["value_ytd"].window_range == "cumulative"
    assert by_key["combo"].operands == ("current_value", "value_ytd")
