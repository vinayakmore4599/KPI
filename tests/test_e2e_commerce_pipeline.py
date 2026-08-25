"""End-to-end: commerce pipeline with named row steps, lookups, entity windows, having.

What this file provides
    One request graph for:
      1–6  named row steps (GST, COD lookup, nested shipping, returns, rebate)
      7    per-customer `over:` windows ordered by order_date
      8    SUM/AVG/COUNT at region × product_category
      9    RANK of region within product_category
     10   having drop + then_group_by category share; predicate flags without drop

Where it is used
    pytest tests/test_e2e_commerce_pipeline.py.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError
from tests.conftest import make_context, write_yaml

TAX = 1.18
REBATE_PCT = {"Gold": 0.05, "Silver": 0.02, "Bronze": 0.0}

# Shared by YAML expr, DuckDB windows, and the pandas oracle.
NET_BEFORE_REBATE = (
    "CASE WHEN is_returned = 1 THEN (0 - quantity * unit_cost) ELSE "
    "quantity * unit_price * (1 - discount_pct) * 1.18 "
    "+ CASE WHEN quantity * unit_price * (1 - discount_pct) < 150 THEN shipping_cost "
    "WHEN quantity * unit_price * (1 - discount_pct) <= 500 THEN shipping_cost * 0.5 "
    "ELSE 0 END "
    "- CASE WHEN payment_method = 'COD' THEN 25 ELSE 10 END END"
)
FINAL_EXPR = (
    f"({NET_BEFORE_REBATE}) * CASE WHEN customer_tier = 'Gold' THEN 0.95 "
    "WHEN customer_tier = 'Silver' THEN 0.98 ELSE 1 END"
)
PROFIT_EXPR = f"({FINAL_EXPR}) - quantity * unit_cost"
MARGIN_EXPR = f"({PROFIT_EXPR}) / nullif(({FINAL_EXPR}), 0)"

WINDOW_SQL = f"""
SELECT
  order_date AS event_month,
  order_date,
  region,
  product_category,
  customer_id,
  order_id,
  quantity,
  unit_price,
  unit_cost,
  discount_pct,
  shipping_cost,
  payment_method,
  is_returned,
  customer_tier,
  ROW_NUMBER() OVER (
    PARTITION BY customer_id ORDER BY order_date, order_id
  ) AS order_sequence_num,
  date_diff(
    'day',
    LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date, order_id),
    order_date
  ) AS days_since_last_order,
  SUM({FINAL_EXPR}) OVER (
    PARTITION BY customer_id ORDER BY order_date, order_id
  ) AS running_total_per_customer
FROM read_parquet($sotif_path)
"""

ORDER_COLUMNS = [
    "event_month",
    "order_date",
    "region",
    "product_category",
    "customer_id",
    "order_id",
    "quantity",
    "unit_price",
    "unit_cost",
    "discount_pct",
    "shipping_cost",
    "payment_method",
    "is_returned",
    "customer_tier",
]

AGG_MEASURES = [
    "total_revenue",
    "total_profit",
    "total_cost",
    "avg_margin_pct",
    "order_count",
    "return_rate",
    "overall_margin",
    "revenue_rank",
    "group_share",
    "healthy",
]

ORDER_MEASURES = [
    "total_revenue",
    "total_profit",
    "avg_margin_pct",
    "order_sequence_num",
    "days_since_last_order",
    "running_total_per_customer",
]


def _line(
    day: int,
    order_id: str,
    customer_id: str,
    region: str,
    category: str,
    quantity: float,
    unit_price: float,
    unit_cost: float,
    discount_pct: float,
    shipping_cost: float,
    payment_method: str,
    is_returned: int,
    tier: str,
) -> dict:
    return {
        "order_date": date(2026, 3, day),
        "event_month": date(2026, 3, 1),
        "region": region,
        "product_category": category,
        "customer_id": customer_id,
        "order_id": order_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "unit_cost": unit_cost,
        "discount_pct": discount_pct,
        "shipping_cost": shipping_cost,
        "payment_method": payment_method,
        "is_returned": is_returned,
        "customer_tier": tier,
    }


def _orders() -> list[dict]:
    """March orders: two healthy NA groups, two failing EU groups, one zero-final return."""
    return [
        _line(1, "O1", "C1", "NA", "Electronics", 10, 80, 40, 0.0, 40, "UPI", 0, "Gold"),
        _line(5, "O2", "C1", "NA", "Electronics", 1, 20, 8, 0.0, 15, "COD", 0, "Gold"),
        _line(12, "O3", "C1", "NA", "Electronics", 5, 40, 20, 0.1, 20, "UPI", 0, "Gold"),
        _line(2, "O4", "C2", "NA", "Apparel", 8, 50, 25, 0.0, 30, "UPI", 0, "Silver"),
        _line(10, "O5", "C2", "NA", "Apparel", 2, 30, 15, 0.0, 12, "COD", 0, "Silver"),
        _line(3, "O6", "C3", "EU", "Electronics", 4, 25, 20, 0.0, 10, "UPI", 1, "Bronze"),
        _line(8, "O7", "C3", "EU", "Electronics", 3, 15, 10, 0.0, 8, "COD", 0, "Bronze"),
        _line(4, "O8", "C4", "EU", "Apparel", 1, 10, 50, 0.0, 20, "UPI", 0, "Gold"),
        _line(6, "O9", "C5", "EU", "Apparel", 1, 0, 0, 0.0, 5, "UPI", 1, "Bronze"),
    ]


def _resolve(row: dict) -> dict:
    """Independent oracle for steps 1–6."""
    gross_revenue = float(row["quantity"]) * float(row["unit_price"])
    gross_cost = float(row["quantity"]) * float(row["unit_cost"])
    discounted = gross_revenue * (1.0 - float(row["discount_pct"]))
    taxed = discounted * TAX
    fee = 25.0 if row["payment_method"] == "COD" else 10.0
    if discounted < 150:
        shipping_adj = float(row["shipping_cost"])
    elif discounted <= 500:
        shipping_adj = float(row["shipping_cost"]) * 0.5
    else:
        shipping_adj = 0.0
    if int(row["is_returned"]) == 1:
        net = -gross_cost
    else:
        net = taxed + shipping_adj - fee
    final = net * (1.0 - REBATE_PCT[row["customer_tier"]])
    profit = final - gross_cost
    margin = None if final == 0 else profit / final
    return {
        "gross_cost": gross_cost,
        "final": final,
        "profit": profit,
        "margin": margin,
        "is_returned": float(row["is_returned"]),
    }


def _with_resolved(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    work["order_date"] = pd.to_datetime(work["order_date"]).dt.date
    return pd.DataFrame([{**row, **_resolve(row)} for row in work.to_dict("records")])


def _customer_windows(frame: pd.DataFrame) -> pd.DataFrame:
    """Date-ordered ROW_NUMBER / running SUM / LAG days per customer."""
    work = _with_resolved(frame).sort_values(["customer_id", "order_date", "order_id"])
    parts = []
    for _, chunk in work.groupby("customer_id", sort=False):
        item = chunk.copy()
        item["order_sequence_num"] = range(1, len(item) + 1)
        item["running_total_per_customer"] = item["final"].cumsum()
        prev = item["order_date"].shift(1)
        item["days_since_last_order"] = [
            None if pd.isna(before) else (day - before).days
            for day, before in zip(item["order_date"], prev)
        ]
        parts.append(item)
    return pd.concat(parts, ignore_index=True)


def _group_oracle(frame: pd.DataFrame, keys: tuple[str, ...]) -> list[dict]:
    work = _with_resolved(frame)
    if keys:
        grouped = work.groupby(list(keys), dropna=False)
        chunks = [
            (dict(zip(keys, name if isinstance(name, tuple) else (name,))), chunk)
            for name, chunk in grouped
        ]
    else:
        chunks = [({}, work)]
    rows = []
    for dims, chunk in chunks:
        item = dict(dims)
        item["total_revenue"] = float(chunk["final"].sum())
        item["total_profit"] = float(chunk["profit"].sum())
        item["total_cost"] = float(chunk["gross_cost"].sum())
        margins = chunk["margin"].dropna()
        item["avg_margin_pct"] = float(margins.mean()) if len(margins) else None
        item["order_count"] = float(chunk["order_id"].nunique())
        item["return_rate"] = float(chunk["is_returned"].mean())
        denom = item["total_revenue"]
        item["overall_margin"] = None if denom == 0 else item["total_profit"] / denom
        item["profit_ok"] = 1.0 if item["total_profit"] > 0 else 0.0
        item["returns_ok"] = 1.0 if item["return_rate"] < 0.20 else 0.0
        item["healthy"] = item["profit_ok"] * item["returns_ok"]
        rows.append(item)
    return rows


def _rank_within_category(rows: list[dict]) -> None:
    by_cat: dict[str, list[dict]] = {}
    for row in rows:
        by_cat.setdefault(row["product_category"], []).append(row)
    for group in by_cat.values():
        ordered = sorted(group, key=lambda row: -row["total_revenue"])
        for index, row in enumerate(ordered, start=1):
            row["revenue_rank"] = index
    total = sum(row["total_revenue"] for row in rows)
    for row in rows:
        row["group_share"] = None if total == 0 else row["total_revenue"] / total * 100


def _surviving_category_share(rows: list[dict]) -> dict[str, float]:
    kept = [row for row in rows if row["healthy"] == 1.0]
    by_cat: dict[str, float] = {}
    for row in kept:
        by_cat[row["product_category"]] = (
            by_cat.get(row["product_category"], 0.0) + row["total_revenue"]
        )
    grand = sum(by_cat.values())
    return {name: value / grand * 100 for name, value in by_cat.items()}


def _pick(result: dict, **dims: str) -> dict:
    matches = [
        row
        for row in result["rows"]
        if all(row.get(name) == value for name, value in dims.items())
    ]
    assert matches, (dims, result["rows"])
    assert len(matches) == 1, (dims, matches)
    return matches[0]


def _output_schema() -> list[dict]:
    names = [
        "event_month",
        "order_date",
        "region",
        "product_category",
        "customer_id",
        "order_id",
        "quantity",
        "unit_price",
        "unit_cost",
        "discount_pct",
        "shipping_cost",
        "payment_method",
        "is_returned",
        "customer_tier",
        "order_sequence_num",
        "days_since_last_order",
        "running_total_per_customer",
    ]
    return [{"name": name, "type": "varchar"} for name in names]


def _pipeline_kpi(kpi_id: int, *, having: bool = False, rollup: bool = False) -> dict:
    having_block = None
    if having:
        having_block = {
            "match": "all",
            "predicates": [
                {"of": "total_profit", "cmp": "gt", "value": 0},
                {"of": "return_rate", "cmp": "lt", "value": 0.20},
            ],
        }
        if rollup:
            having_block["then_group_by"] = ["product_category"]
    spec = {
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
            {"name": "region", "from": "region"},
            {"name": "product_category", "from": "product_category"},
            {"name": "customer_id", "from": "customer_id"},
            {"name": "order_id", "from": "order_id"},
        ],
        "default_dimensions": ["region", "product_category"],
        "base_measures": {
            "gross_revenue": {"expr": "quantity * unit_price"},
            "discounted": {"expr": "gross_revenue * (1 - discount_pct)"},
            "taxed": {"expr": "discounted * 1.18"},
            "platform_fee": {
                "lookup": {"column": "payment_method", "map": {"COD": 25}, "default": 10},
            },
            "shipping_adj": {
                "expr": (
                    "CASE WHEN discounted < 150 THEN shipping_cost "
                    "WHEN discounted <= 500 THEN shipping_cost * 0.5 ELSE 0 END"
                ),
            },
            "net_before_rebate": {
                "expr": (
                    "CASE WHEN is_returned = 1 THEN (0 - quantity * unit_cost) "
                    "ELSE taxed + shipping_adj - platform_fee END"
                ),
            },
            "rebate_pct": {
                "lookup": {
                    "column": "customer_tier",
                    "map": {"Gold": 0.05, "Silver": 0.02, "Bronze": 0},
                    "default": 0,
                },
            },
            "final_amount": {"expr": "net_before_rebate * (1 - rebate_pct)", "agg": "sum"},
            "profit_amount": {
                "expr": "final_amount - quantity * unit_cost",
                "agg": "sum",
            },
            "cost_amount": {"expr": "quantity * unit_cost", "agg": "sum"},
            "margin_row": {
                "expr": "(final_amount - quantity * unit_cost) / nullif(final_amount, 0)",
                "agg": "avg",
            },
            "n_orders": {"sql": "order_id", "agg": "count_distinct"},
            "return_avg": {"sql": "is_returned", "agg": "avg"},
            "prev_order_date": {
                "over": {
                    "fn": "lag",
                    "of": "order_date",
                    "partition_by": ["customer_id"],
                    "order_by": ["order_date", "order_id"],
                },
                "agg": "max",
            },
            "seq_num": {
                "over": {
                    "fn": "row_number",
                    "partition_by": ["customer_id"],
                    "order_by": ["order_date", "order_id"],
                },
                "agg": "max",
            },
            "gap_days": {
                "expr": "date_diff(prev_order_date, order_date, 'day')",
                "agg": "max",
            },
            "running_final": {
                "over": {
                    "fn": "running_sum",
                    "of": "final_amount",
                    "partition_by": ["customer_id"],
                    "order_by": ["order_date", "order_id"],
                },
                "agg": "max",
            },
        },
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "total_revenue": {"of": "final_amount", "op": "point", "offset": {"months": 0}},
            "total_profit": {"of": "profit_amount", "op": "point", "offset": {"months": 0}},
            "total_cost": {"of": "cost_amount", "op": "point", "offset": {"months": 0}},
            "avg_margin_pct": {"of": "margin_row", "op": "point", "offset": {"months": 0}},
            "order_count": {"of": "n_orders", "op": "point", "offset": {"months": 0}},
            "return_rate": {"of": "return_avg", "op": "point", "offset": {"months": 0}},
            "overall_margin": {
                "op": "expr",
                "expr": "total_profit / nullif(total_revenue, 0)",
            },
            "revenue_rank": {
                "op": "rank",
                "of": "total_revenue",
                "order": "desc",
                "partition_by": ["product_category"],
                "cuts": ["G"],
            },
            "group_share": {
                "op": "percent_of_total",
                "of": "total_revenue",
                "cuts": ["G"],
            },
            "healthy": {
                "op": "predicate",
                "match": "all",
                "predicates": [
                    {"of": "total_profit", "cmp": "gt", "value": 0},
                    {"of": "return_rate", "cmp": "lt", "value": 0.20},
                ],
            },
            "order_sequence_num": {"of": "seq_num", "op": "point", "offset": {"months": 0}},
            "days_since_last_order": {"of": "gap_days", "op": "point", "offset": {"months": 0}},
            "running_total_per_customer": {
                "of": "running_final",
                "op": "point",
                "offset": {"months": 0},
            },
        },
    }
    if having_block is not None:
        spec["having"] = having_block
    return spec


def _write_sql_window_model(extra_config) -> None:
    write_yaml(
        extra_config / "models" / "orders_win.yaml",
        {
            "model_id": "orders_win",
            "kind": "sql",
            "required_aliases": ["sotif"],
            "output_schema": _output_schema(),
            "sql": WINDOW_SQL.strip() + "\n",
        },
    )


def _context(
    path,
    extra_config,
    kpi_id,
    measures,
    *,
    selected_dimensions=None,
    having=False,
    rollup=False,
):
    write_yaml(
        extra_config / "kpis" / f"{kpi_id}.yaml",
        _pipeline_kpi(kpi_id, having=having, rollup=rollup),
    )
    ctx = make_context(
        path,
        measures=measures,
        kpi_id=kpi_id,
        month="2026-03",
        selected_dimensions=selected_dimensions,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(ORDER_COLUMNS)
    return ctx


def _approx(actual, expected, key: str) -> None:
    if expected is None:
        assert actual is None or (isinstance(actual, float) and pd.isna(actual)), (key, actual)
        return
    assert actual == pytest.approx(expected), (key, actual, expected)


def test_e2e_commerce_steps_agg_rank_predicate(tmp_path, extra_config):
    """Named row steps + lookup + predicate flags at region × category (no drop)."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    ctx = _context(path, extra_config, 9960, AGG_MEASURES)
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"] == result["sql"]
    assert "ROW_NUMBER()" not in result["sql"]
    assert result["selected_dimensions"] == ["region", "product_category"]

    expected = _group_oracle(orders, ("region", "product_category"))
    _rank_within_category(expected)
    assert len(result["rows"]) == 4
    for exp in expected:
        row = _pick(result, region=exp["region"], product_category=exp["product_category"])
        assert row["grouped_dimensions"] == ["region", "product_category"]
        for key in AGG_MEASURES:
            _approx(row[key], exp[key], key)

    na_elec = _pick(result, region="NA", product_category="Electronics")
    eu_elec = _pick(result, region="EU", product_category="Electronics")
    na_app = _pick(result, region="NA", product_category="Apparel")
    eu_app = _pick(result, region="EU", product_category="Apparel")
    assert na_elec["revenue_rank"] == 1
    assert eu_elec["revenue_rank"] == 2
    assert na_app["revenue_rank"] == 1
    assert eu_app["revenue_rank"] == 2
    assert na_elec["healthy"] == 1.0
    assert na_app["healthy"] == 1.0
    assert eu_elec["healthy"] == 0.0
    assert eu_app["healthy"] == 0.0
    assert na_elec["overall_margin"] != pytest.approx(na_elec["avg_margin_pct"])


def test_e2e_commerce_customer_windows_by_order_date(tmp_path, extra_config):
    """Step 7: sequence, running final, and days-since-last at customer × order grain."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    ctx = _context(
        path,
        extra_config,
        9961,
        ORDER_MEASURES,
        selected_dimensions=["customer_id", "order_id"],
    )
    result = compute(ctx, config_dir=extra_config)
    windows = _customer_windows(orders)
    assert result["selected_dimensions"] == ["customer_id", "order_id"]
    assert len(result["rows"]) == len(orders)

    c1 = windows[windows["customer_id"] == "C1"].sort_values("order_date")
    assert list(c1["order_sequence_num"]) == [1, 2, 3]
    gaps = [None if pd.isna(value) else int(value) for value in c1["days_since_last_order"]]
    assert gaps == [None, 4, 7]
    o1 = _pick(result, customer_id="C1", order_id="O1")
    o2 = _pick(result, customer_id="C1", order_id="O2")
    o3 = _pick(result, customer_id="C1", order_id="O3")
    assert o1["grouped_dimensions"] == ["customer_id", "order_id"]
    assert o1.get("region") is None
    _approx(o1["order_sequence_num"], 1, "seq")
    _approx(o2["order_sequence_num"], 2, "seq")
    _approx(o3["order_sequence_num"], 3, "seq")
    _approx(o1["days_since_last_order"], None, "gap")
    _approx(o2["days_since_last_order"], 4, "gap")
    _approx(o3["days_since_last_order"], 7, "gap")
    _approx(o1["running_total_per_customer"], float(c1.iloc[0]["running_total_per_customer"]), "run")
    _approx(o2["running_total_per_customer"], float(c1.iloc[1]["running_total_per_customer"]), "run")
    _approx(o3["running_total_per_customer"], float(c1.iloc[2]["running_total_per_customer"]), "run")
    _approx(o1["total_revenue"], float(c1.iloc[0]["final"]), "final")
    assert o3["running_total_per_customer"] == pytest.approx(
        o1["total_revenue"] + o2["total_revenue"] + o3["total_revenue"]
    )

    zero = _pick(result, customer_id="C5", order_id="O9")
    _approx(zero["total_revenue"], 0.0, "zero_final")
    _approx(zero["avg_margin_pct"], None, "nullif_guard")


def test_e2e_commerce_region_rollup_and_partition_bind(tmp_path, extra_config):
    """SUM rolls to [region]; AVG is not the mean of category avgs; partition_by needs grain."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    region_measures = [m for m in AGG_MEASURES if m != "revenue_rank"]
    ctx = _context(path, extra_config, 9962, region_measures, selected_dimensions=["region"])
    result = compute(ctx, config_dir=extra_config)
    expected = _group_oracle(orders, ("region",))
    na = _pick(result, region="NA")
    eu = _pick(result, region="EU")
    na_e = next(row for row in expected if row["region"] == "NA")
    eu_e = next(row for row in expected if row["region"] == "EU")
    assert na["grouped_dimensions"] == ["region"]
    assert na.get("product_category") is None
    _approx(na["total_revenue"], na_e["total_revenue"], "na_rev")
    _approx(na["total_profit"], na_e["total_profit"], "na_profit")
    _approx(na["order_count"], na_e["order_count"], "na_n")
    _approx(eu["total_revenue"], eu_e["total_revenue"], "eu_rev")
    assert na["healthy"] == 1.0
    assert eu["healthy"] == 0.0

    cat_ctx = _context(path, extra_config, 9962, ["total_revenue", "avg_margin_pct", "order_count"])
    by_cat = compute(cat_ctx, config_dir=extra_config)
    na_elec = _pick(by_cat, region="NA", product_category="Electronics")
    na_app = _pick(by_cat, region="NA", product_category="Apparel")
    assert na["total_revenue"] == pytest.approx(na_elec["total_revenue"] + na_app["total_revenue"])
    mean_of_avgs = (na_elec["avg_margin_pct"] + na_app["avg_margin_pct"]) / 2
    assert na["avg_margin_pct"] != pytest.approx(mean_of_avgs)

    ranked = _context(
        path,
        extra_config,
        9963,
        ["total_revenue", "revenue_rank"],
        selected_dimensions=["region"],
    )
    with pytest.raises(BindError, match="partition_by 'product_category' is not in cut 'G'"):
        compute(ranked, config_dir=extra_config)


def test_e2e_commerce_having_then_group_by_category_share(tmp_path, extra_config):
    """HAVING drops unhealthy region×category groups, then re-folds to category share."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    ctx = _context(
        path,
        extra_config,
        9964,
        ["total_revenue", "total_profit", "return_rate", "group_share"],
        having=True,
        rollup=True,
    )
    result = compute(ctx, config_dir=extra_config)
    assert {row["product_category"] for row in result["rows"]} == {"Electronics", "Apparel"}
    assert all(row["region"] is None for row in result["rows"])
    expected = _group_oracle(orders, ("region", "product_category"))
    shares = _surviving_category_share(expected)
    for row in result["rows"]:
        _approx(row["group_share"], shares[row["product_category"]], "share")
    assert len(result["dropped_groups"]) >= 2
    assert {item["reason"] for item in result["dropped_groups"]} == {"having"}


def test_e2e_commerce_sql_cte_window_still_legal(tmp_path, extra_config):
    """A KPI may still compute entity windows in a kind: sql model."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    _write_sql_window_model(extra_config)
    spec = _pipeline_kpi(9965)
    spec["model"] = "orders_win"
    spec["base_measures"] = {
        "final_amount": {"sql": "running_total_per_customer", "agg": "max"},
        "seq_num": {"sql": "order_sequence_num", "agg": "max"},
    }
    spec["measures"] = {
        "running_total_per_customer": {"of": "final_amount", "op": "point"},
        "order_sequence_num": {"of": "seq_num", "op": "point"},
    }
    write_yaml(extra_config / "kpis" / "9965.yaml", spec)
    ctx = make_context(
        path,
        measures=["running_total_per_customer", "order_sequence_num"],
        kpi_id=9965,
        month="2026-03",
        selected_dimensions=["customer_id", "order_id"],
    )
    ctx["datasets"]["Sotif"]["columns"] = list(ORDER_COLUMNS)
    result = compute(ctx, config_dir=extra_config)
    assert "ROW_NUMBER()" in result["sql"]
    o1 = _pick(result, customer_id="C1", order_id="O1")
    assert o1["order_sequence_num"] == pytest.approx(1)
