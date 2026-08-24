"""End-to-end: multi-step row math, chained measures, aggs, grains, two models.

What this file provides
    A net-revenue pipeline that the kitchen-sink Sotif tests do not cover:
    row-level qty × price → discount → tax → min-order shipping → tier rebate,
    then SUM/AVG/COUNT/MAX at a request grain, a 2- and 3-measure expr graph,
    rank + a having-style floor flag, and a second-model target join.

    apply: result cannot drop rows by a measure (HAVING). Groups below the
    floor stay in the payload; clears_floor is 1 only when total > 1000.

Where it is used
    pytest tests/test_e2e_multistep_revenue.py.

When to use
    Add a case when row-then-agg formulas, post-agg measure graphs, or
    multi-model joins at selected_dimensions change.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from tests.conftest import make_context, write_yaml

ANCHOR = date(2026, 3, 1)
TAX = 1.18
MIN_ORDER = 200.0
REBATE = {"Gold": 15.0, "Silver": 7.0, "Bronze": 0.0}

GROSS_EXPR = "quantity * unit_price"
DISC_EXPR = "quantity * unit_price * (1 - discount_pct)"
TAX_EXPR = "quantity * unit_price * (1 - discount_pct) * 1.18"
FINAL_EXPR = (
    "CASE WHEN quantity * unit_price * (1 - discount_pct) < 200 "
    "THEN quantity * unit_price * (1 - discount_pct) * 1.18 + shipping_cost "
    "ELSE quantity * unit_price * (1 - discount_pct) * 1.18 END "
    "- CASE WHEN customer_tier = 'Gold' THEN 15 "
    "WHEN customer_tier = 'Silver' THEN 7 ELSE 0 END"
)

ORDER_COLUMNS = [
    "event_month",
    "region",
    "product_category",
    "order_id",
    "quantity",
    "unit_price",
    "discount_pct",
    "shipping_cost",
    "customer_tier",
]

MEASURES = [
    "gross_total",
    "discounted_total",
    "taxed_total",
    "total_final_revenue",
    "avg_order_value",
    "order_count",
    "max_single_order",
    "tax_add",
    "avg_from_ratio",
    "intensity",
    "clears_floor",
    "revenue_rank",
    "target_now",
    "attainment",
]


def _orders() -> list[dict]:
    """Eight orders: two region × category groups clear 1000, two do not."""
    return [
        _line("O1", "NA", "Electronics", 20, 50, 0.10, 20, "Gold"),
        _line("O2", "NA", "Electronics", 5, 10, 0.00, 25, "Silver"),
        _line("O3", "NA", "Electronics", 3, 100, 0.05, 10, "Gold"),
        _line("O4", "EU", "Electronics", 4, 80, 0.00, 30, "Bronze"),
        _line("O5", "EU", "Electronics", 1, 90, 0.10, 40, "Gold"),
        _line("O6", "NA", "Apparel", 15, 40, 0.00, 5, "Silver"),
        _line("O7", "NA", "Apparel", 10, 40, 0.20, 8, "Bronze"),
        _line("O8", "EU", "Apparel", 1, 20, 0.00, 15, "Bronze"),
    ]


def _line(
    order_id: str,
    region: str,
    category: str,
    quantity: float,
    unit_price: float,
    discount_pct: float,
    shipping_cost: float,
    tier: str,
) -> dict:
    return {
        "event_month": ANCHOR,
        "region": region,
        "product_category": category,
        "order_id": order_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "discount_pct": discount_pct,
        "shipping_cost": shipping_cost,
        "customer_tier": tier,
    }


def _resolve(row: dict) -> dict:
    """Independent oracle for steps 1–5 (row-level)."""
    gross = float(row["quantity"]) * float(row["unit_price"])
    discounted = gross * (1.0 - float(row["discount_pct"]))
    taxed = discounted * TAX
    net = taxed + float(row["shipping_cost"]) if discounted < MIN_ORDER else taxed
    final = net - REBATE[row["customer_tier"]]
    return {
        "gross": gross,
        "discounted": discounted,
        "taxed": taxed,
        "final": final,
    }


def _oracle(frame: pd.DataFrame, keys: tuple[str, ...]) -> list[dict]:
    """Group resolved rows and attach SUM/AVG/COUNT/MAX plus post-agg graph."""
    work = frame.copy()
    work["event_month"] = pd.to_datetime(work["event_month"]).dt.date
    resolved = pd.DataFrame([{**row, **_resolve(row)} for row in work.to_dict("records")])
    if keys:
        grouped = resolved.groupby(list(keys), dropna=False)
        chunks = [(dict(zip(keys, name if isinstance(name, tuple) else (name,))), chunk) for name, chunk in grouped]
    else:
        chunks = [({}, resolved)]
    rows = []
    for dims, chunk in chunks:
        item = dict(dims)
        item["gross_total"] = float(chunk["gross"].sum())
        item["discounted_total"] = float(chunk["discounted"].sum())
        item["taxed_total"] = float(chunk["taxed"].sum())
        item["total_final_revenue"] = float(chunk["final"].sum())
        item["avg_order_value"] = float(chunk["final"].mean())
        item["order_count"] = float(chunk["order_id"].nunique())
        item["max_single_order"] = float(chunk["final"].max())
        item["tax_add"] = item["taxed_total"] - item["discounted_total"]
        item["avg_from_ratio"] = item["total_final_revenue"] / item["order_count"]
        item["intensity"] = (item["total_final_revenue"] + item["max_single_order"]) / item["order_count"]
        item["clears_floor"] = 1.0 if item["total_final_revenue"] > 1000 else 0.0
        rows.append(item)
    rows.sort(key=lambda row: -row["total_final_revenue"])
    for index, row in enumerate(rows, start=1):
        row["revenue_rank"] = index
    return rows


def _targets() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"event_month": ANCHOR, "region": "NA", "product_category": "Electronics", "target_revenue": 2000},
            {"event_month": ANCHOR, "region": "NA", "product_category": "Apparel", "target_revenue": 800},
            {"event_month": ANCHOR, "region": "EU", "product_category": "Electronics", "target_revenue": 400},
            {"event_month": ANCHOR, "region": "EU", "product_category": "Apparel", "target_revenue": 100},
        ]
    )


def _target_oracle(frame: pd.DataFrame, keys: tuple[str, ...]) -> dict[tuple, float]:
    work = frame.copy()
    work["event_month"] = pd.to_datetime(work["event_month"]).dt.date
    if keys:
        grouped = work.groupby(list(keys), dropna=False)["target_revenue"].sum()
        return {name if isinstance(name, tuple) else (name,): float(value) for name, value in grouped.items()}
    return {(): float(work["target_revenue"].sum())}


def _pick(result: dict, **dims: str) -> dict:
    matches = [row for row in result["rows"] if all(row.get(name) == value for name, value in dims.items())]
    assert matches, (dims, result["rows"])
    assert len(matches) == 1, (dims, matches)
    return matches[0]


def _key_tuple(row: dict, keys: tuple[str, ...]) -> tuple:
    return tuple(row[name] for name in keys)


def _revenue_kpi(kpi_id: int, *, having: bool = False) -> dict:
    """Orders extract plus category targets; math is row expr then agg, then measure graph."""
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
        ],
        "default_dimensions": ["region", "product_category"],
        "base_measures": {
            "gross_amount": {"expr": GROSS_EXPR, "model": "sotif"},
            "discounted_amount": {
                "expr": "gross_amount * (1 - discount_pct)",
                "model": "sotif",
            },
            "taxed_amount": {"expr": "discounted_amount * 1.18", "model": "sotif"},
            "gross_value": {"expr": GROSS_EXPR, "agg": "sum", "model": "sotif"},
            "discounted_value": {"expr": "discounted_amount", "agg": "sum", "model": "sotif"},
            "taxed_value": {"expr": "taxed_amount", "agg": "sum", "model": "sotif"},
            "final_sum": {"expr": FINAL_EXPR, "agg": "sum", "model": "sotif"},
            "final_avg": {"expr": FINAL_EXPR, "agg": "avg", "model": "sotif"},
            "final_max": {"expr": FINAL_EXPR, "agg": "max", "model": "sotif"},
            "n_orders": {"sql": "order_id", "agg": "count_distinct", "model": "sotif"},
            "target_value": {"sql": "target_revenue", "agg": "sum", "model": "targets"},
        },
        "model_relations": [
            {
                "left": "final_sum",
                "right": "target_value",
                "on": ["event_month", "region", "product_category"],
                "how": "inner",
            }
        ],
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "gross_total": {"of": "gross_value", "op": "point", "offset": {"months": 0}},
            "discounted_total": {
                "of": "discounted_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "taxed_total": {"of": "taxed_value", "op": "point", "offset": {"months": 0}},
            "total_final_revenue": {"of": "final_sum", "op": "point", "offset": {"months": 0}},
            "avg_order_value": {"of": "final_avg", "op": "point", "offset": {"months": 0}},
            "order_count": {"of": "n_orders", "op": "point", "offset": {"months": 0}},
            "max_single_order": {"of": "final_max", "op": "point", "offset": {"months": 0}},
            "tax_add": {
                "op": "expr",
                "expr": "taxed_total - discounted_total",
            },
            "avg_from_ratio": {
                "op": "expr",
                "expr": "total_final_revenue / order_count",
            },
            "intensity": {
                "op": "expr",
                "expr": "(total_final_revenue + max_single_order) / order_count",
            },
            "clears_floor": {
                "op": "threshold",
                "of": "total_final_revenue",
                "cmp": "gt",
                "value": 1000,
            },
            "revenue_rank": {
                "op": "rank",
                "of": "total_final_revenue",
                "order": "desc",
                "cuts": ["G"],
            },
            "target_now": {"of": "target_value", "op": "point", "offset": {"months": 0}},
            "attainment": {
                "op": "fn",
                "fn": "attainment",
                "inputs": ["total_final_revenue", "target_now"],
            },
        },
    }
    if having:
        spec["having"] = {
            "match": "all",
            "predicates": [{"of": "total_final_revenue", "cmp": "gt", "value": 1000}],
        }
    return spec


def _write_models(extra_config) -> None:
    write_yaml(
        extra_config / "models" / "targets.yaml",
        {
            "model_id": "targets",
            "kind": "physical",
            "required_aliases": ["targets"],
            "sources": {"targets": {"alias": "targets"}},
            "joins": [],
        },
    )


def _context(
    path, targets_path, extra_config, kpi_id, *, selected_dimensions=None, having=False
):
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", _revenue_kpi(kpi_id, having=having))
    _write_models(extra_config)
    ctx = make_context(
        path,
        measures=MEASURES,
        kpi_id=kpi_id,
        selected_dimensions=selected_dimensions,
        extra_datasets={
            "Targets": {
                "dataset_id": 41,
                "dataset_name": "TARGETS",
                "table_type": "PARQUET",
                "path": str(targets_path),
                "alias": "targets",
                "columns": ["event_month", "region", "product_category", "target_revenue"],
                "filter_column_mappings": [],
            }
        },
    )
    ctx["datasets"]["Sotif"]["columns"] = ORDER_COLUMNS
    return ctx


def _approx_row(actual: dict, expected: dict, keys: list[str]) -> None:
    for key in keys:
        assert actual[key] == pytest.approx(expected[key]), (key, actual[key], expected[key])


def test_e2e_net_revenue_steps_aggs_rank_at_region_category(tmp_path, extra_config):
    """Steps 1–5 on each order, step 7 aggs, chained measures, rank, target join."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    targets = _targets()
    tpath = tmp_path / "targets.parquet"
    targets.to_parquet(tpath, index=False)

    ctx = _context(path, tpath, extra_config, 9950, having=True)
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"] == result["sql"]
    assert result["selected_dimensions"] == ["region", "product_category"]
    assert len(result["sqls"]) == 2

    expected = _oracle(orders, ("region", "product_category"))
    target_by = _target_oracle(targets, ("region", "product_category"))
    kept_exp = [exp for exp in expected if exp["total_final_revenue"] > 1000]
    assert len(result["rows"]) == len(kept_exp)
    for exp in kept_exp:
        row = _pick(result, region=exp["region"], product_category=exp["product_category"])
        assert row["grouped_dimensions"] == ["region", "product_category"]
        _approx_row(row, exp, [k for k in MEASURES if k not in {"target_now", "attainment"}])
        tkey = (exp["region"], exp["product_category"])
        assert row["target_now"] == pytest.approx(target_by[tkey])
        assert row["attainment"] == pytest.approx(exp["total_final_revenue"] / target_by[tkey] * 100)
        assert row["avg_from_ratio"] == pytest.approx(row["avg_order_value"])
        assert row["tax_add"] == pytest.approx(row["discounted_total"] * 0.18)

    na_elec = _pick(result, region="NA", product_category="Electronics")
    na_app = _pick(result, region="NA", product_category="Apparel")
    assert na_elec["revenue_rank"] == 1
    assert na_app["revenue_rank"] == 2
    assert na_elec["clears_floor"] == 1.0
    assert {(r["region"], r["product_category"]) for r in result["rows"]} == {
        ("NA", "Electronics"),
        ("NA", "Apparel"),
    }
    assert result["dropped_groups"]
    o1 = _resolve(_orders()[0])
    assert na_elec["max_single_order"] == pytest.approx(o1["final"])
    assert na_elec["max_single_order"] == pytest.approx(1047.0)


def test_e2e_net_revenue_region_and_worldwide_grains(tmp_path, extra_config):
    """Same graph at [region] and []; SUM rolls up, AVG is not the mean of category avgs."""
    orders = pd.DataFrame(_orders())
    path = tmp_path / "orders.parquet"
    orders.to_parquet(path, index=False)
    tpath = tmp_path / "targets.parquet"
    _targets().to_parquet(tpath, index=False)

    ctx = _context(path, tpath, extra_config, 9951)
    by_cat = compute(ctx, config_dir=extra_config)
    by_region = compute({**ctx, "selected_dimensions": ["region"]}, config_dir=extra_config)
    worldwide = compute({**ctx, "selected_dimensions": []}, config_dir=extra_config)

    region_exp = _oracle(orders, ("region",))
    world_exp = _oracle(orders, ())
    region_targets = _target_oracle(_targets(), ("region",))
    world_targets = _target_oracle(_targets(), ())

    na = _pick(by_region, region="NA")
    eu = _pick(by_region, region="EU")
    na_e = next(r for r in region_exp if r["region"] == "NA")
    eu_e = next(r for r in region_exp if r["region"] == "EU")
    assert na["grouped_dimensions"] == ["region"]
    assert na["product_category"] is None
    _approx_row(na, na_e, ["total_final_revenue", "avg_order_value", "order_count", "max_single_order"])
    assert na["revenue_rank"] == 1
    assert eu["revenue_rank"] == 2
    assert na["clears_floor"] == 1.0
    assert eu["clears_floor"] == 0.0
    assert na["target_now"] == pytest.approx(region_targets[("NA",)])
    assert na["attainment"] == pytest.approx(na_e["total_final_revenue"] / region_targets[("NA",)] * 100)

    na_elec = _pick(by_cat, region="NA", product_category="Electronics")
    na_app = _pick(by_cat, region="NA", product_category="Apparel")
    assert na["total_final_revenue"] == pytest.approx(
        na_elec["total_final_revenue"] + na_app["total_final_revenue"]
    )
    mean_of_avgs = (na_elec["avg_order_value"] + na_app["avg_order_value"]) / 2
    assert na["avg_order_value"] != pytest.approx(mean_of_avgs)
    assert na["order_count"] == pytest.approx(na_elec["order_count"] + na_app["order_count"])

    world = worldwide["rows"][0]
    assert world["grouped_dimensions"] == []
    assert world["region"] is None
    _approx_row(world, world_exp[0], ["total_final_revenue", "avg_order_value", "order_count"])
    assert world["revenue_rank"] == 1
    assert world["clears_floor"] == 1.0
    assert world["target_now"] == pytest.approx(world_targets[()])
    assert world["total_final_revenue"] == pytest.approx(
        na["total_final_revenue"] + eu["total_final_revenue"]
    )
    assert by_cat["selected_dimensions"] == ["region", "product_category"]
    assert by_region["selected_dimensions"] == ["region"]
    assert worldwide["selected_dimensions"] == []


def test_e2e_net_revenue_validate_then_compute_at_each_grain(tmp_path, extra_config):
    """validate SQL matches compute at default, region, and empty grains."""
    path = tmp_path / "orders.parquet"
    pd.DataFrame(_orders()).to_parquet(path, index=False)
    tpath = tmp_path / "targets.parquet"
    _targets().to_parquet(tpath, index=False)
    base = _context(path, tpath, extra_config, 9952)
    for overlay in (None, ["region"], []):
        ctx = base if overlay is None else {**base, "selected_dimensions": overlay}
        planned = validate(ctx, config_dir=extra_config)
        computed = compute(ctx, config_dir=extra_config)
        assert planned["sql"] == computed["sql"]
        assert planned["sqls"] == computed["sqls"]
        assert planned["selected_dimensions"] == computed["selected_dimensions"]
        for row in computed["rows"]:
            for key in MEASURES:
                assert key in row, (key, overlay, row)
