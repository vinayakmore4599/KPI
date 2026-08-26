"""End-to-end: last-mile SLA settlement with identity helpers and date math.

What this file provides
    One shipment fact that the kitchen-sink / commerce tests do not cover:
      1  named row steps (line-haul, fuel lookup, late days from date_diff(start, end))
      2  identity_grain helpers as point `of` (transit, late, on-time, net)
      3  last_n charge lists and running billed per carrier
      4  measure-level date_add (ISO), date_diff, epoch_day
      5  region × lane SUM/AVG/COUNT, rank, share, predicate, green_when
      6  fill_zero densify of a Feb-only lane, dropped by having at March
      7  snapshot open-claims KPI that keeps host reporting_month (reason no_time)

    Numbers are checked against an independent pandas oracle, not another engine path.

Where it is used
    pytest tests/test_e2e_freight_sla.py.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError
from tests.conftest import make_context, write_yaml, unwrap_cell, value_of

FUEL_PCT = {"NA": 0.12, "EU": 0.18}
LATE_PENALTY = 15.0
REVIEW_DAYS = 7
ANCHOR = date(2026, 3, 1)
EPOCH = date(1970, 1, 1)

SHIP_COLUMNS = [
    "event_month",
    "region",
    "lane",
    "carrier_id",
    "shipment_id",
    "start",
    "end",
    "weight_kg",
    "base_rate",
    "promised_days",
    "fuel_zone",
]

IDENTITY_MEASURES = [
    "transit_days_now",
    "late_days_now",
    "net_now",
    "on_time_now",
    "recent_charges",
    "running_billed",
    "delivered_on",
    "next_review",
    "transit_check",
    "epoch_delivered",
]

SETTLEMENT_MEASURES = [
    "total_charge",
    "charge_2m",
    "avg_transit",
    "on_time_rate",
    "n_shipments",
    "charge_rank",
    "group_share",
    "sla_ok",
    "healthy",
]


def _ship(
    *,
    month: int,
    pickup_day: int,
    delivery_day: int,
    region: str,
    lane: str,
    carrier: str,
    shipment: str,
    weight: float,
    rate: float,
    promised: int,
) -> dict:
    return {
        "event_month": date(2026, month, 1),
        "region": region,
        "lane": lane,
        "carrier_id": carrier,
        "shipment_id": shipment,
        "start": date(2026, month, pickup_day),
        "end": date(2026, month, delivery_day),
        "weight_kg": weight,
        "base_rate": rate,
        "promised_days": promised,
        "fuel_zone": region,
    }


def _shipments() -> list[dict]:
    """March lanes plus one Feb-only Economy row so densify × having is visible."""
    return [
        _ship(
            month=3, pickup_day=1, delivery_day=3, region="NA", lane="Express",
            carrier="CAR-A", shipment="SHP-1", weight=10, rate=4, promised=2,
        ),
        _ship(
            month=3, pickup_day=2, delivery_day=6, region="NA", lane="Express",
            carrier="CAR-A", shipment="SHP-2", weight=8, rate=5, promised=2,
        ),
        _ship(
            month=3, pickup_day=1, delivery_day=5, region="NA", lane="Standard",
            carrier="CAR-B", shipment="SHP-3", weight=20, rate=3, promised=4,
        ),
        _ship(
            month=3, pickup_day=3, delivery_day=4, region="EU", lane="Express",
            carrier="CAR-A", shipment="SHP-4", weight=5, rate=8, promised=2,
        ),
        _ship(
            month=3, pickup_day=4, delivery_day=12, region="EU", lane="Standard",
            carrier="CAR-C", shipment="SHP-5", weight=12, rate=6, promised=3,
        ),
        _ship(
            month=2, pickup_day=10, delivery_day=12, region="NA", lane="Economy",
            carrier="CAR-B", shipment="SHP-6", weight=15, rate=2, promised=3,
        ),
    ]


def _resolve(row: dict) -> dict:
    """Independent oracle for line-haul, fuel, SLA gap, and net charge."""
    start = row["start"]
    end = row["end"]
    transit = (end - start).days
    late = max(transit - int(row["promised_days"]), 0)
    line_haul = float(row["weight_kg"]) * float(row["base_rate"])
    fuel_amt = line_haul * FUEL_PCT[row["fuel_zone"]]
    net = line_haul + fuel_amt - late * LATE_PENALTY
    on_time = 1.0 if late == 0 else 0.0
    return {
        "transit_days": float(transit),
        "late_days": float(late),
        "net_charge": net,
        "on_time": on_time,
        "delivered_iso": end.isoformat(),
        "next_review": (end + timedelta(days=REVIEW_DAYS)).isoformat(),
        "epoch_delivered": float((end - EPOCH).days),
    }


def _with_resolved(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    work["start"] = pd.to_datetime(work["start"]).dt.date
    work["end"] = pd.to_datetime(work["end"]).dt.date
    return pd.DataFrame([{**row, **_resolve(row)} for row in work.to_dict("records")])


def _identity_oracle(frame: pd.DataFrame) -> dict[str, dict]:
    """March shipments only: last_n and running billed are per carrier, date order."""
    march = _with_resolved(frame)
    march = march[march["event_month"] == ANCHOR].copy()
    march = march.sort_values(["carrier_id", "start", "shipment_id"])
    out: dict[str, dict] = {}
    for _, chunk in march.groupby("carrier_id", sort=False):
        running = 0.0
        history: list[float] = []
        for row in chunk.to_dict("records"):
            running += row["net_charge"]
            history.append(row["net_charge"])
            out[row["shipment_id"]] = {
                **row,
                "recent_charges": history[-2:],
                "running_billed": running,
            }
    return out


def _group_oracle(frame: pd.DataFrame) -> list[dict]:
    """March region × lane totals. Feb-only Economy is absent here (having drops it)."""
    march = _with_resolved(frame)
    march = march[march["event_month"] == ANCHOR]
    rows = []
    for (region, lane), chunk in march.groupby(["region", "lane"], dropna=False):
        total = float(chunk["net_charge"].sum())
        item = {
            "region": region,
            "lane": lane,
            "total_charge": total,
            "avg_transit": float(chunk["transit_days"].mean()),
            "on_time_rate": float(chunk["on_time"].mean()),
            "n_shipments": float(len(chunk)),
            "charge_2m": total,
        }
        item["sla_ok"] = 1.0 if item["on_time_rate"] >= 0.5 else 0.0
        item["pays"] = 1.0 if total > 40 else 0.0
        item["healthy"] = item["sla_ok"] * item["pays"]
        rows.append(item)
    ordered = sorted(rows, key=lambda row: -row["total_charge"])
    grand = sum(row["total_charge"] for row in ordered)
    for index, row in enumerate(ordered, start=1):
        row["charge_rank"] = index
        row["group_share"] = None if grand == 0 else row["total_charge"] / grand * 100
    return ordered


def _charge_2m(frame: pd.DataFrame, region: str, lane: str) -> float:
    work = _with_resolved(frame)
    mask = (
        (work["region"] == region)
        & (work["lane"] == lane)
        & (work["event_month"] >= date(2026, 2, 1))
        & (work["event_month"] <= ANCHOR)
    )
    return float(work.loc[mask, "net_charge"].sum())


def _base_measures() -> dict:
    transit = "date_diff(start, end, 'day')"
    late = (
        f"CASE WHEN {transit} > promised_days THEN {transit} - promised_days ELSE 0 END"
    )
    on_time = f"CASE WHEN {transit} <= promised_days THEN 1 ELSE 0 END"
    return {
        "line_haul": {"expr": "weight_kg * base_rate"},
        "fuel_pct": {
            "lookup": {
                "column": "fuel_zone",
                "map": {"NA": 0.12, "EU": 0.18},
                "default": 0,
            },
        },
        "fuel_amt": {"expr": "line_haul * fuel_pct"},
        "transit_days": {"expr": transit},
        "late_days": {"expr": late},
        "on_time_flag": {"expr": on_time},
        "net_charge": {"expr": f"line_haul + fuel_amt - late_days * {LATE_PENALTY}"},
        "billed": {"expr": "net_charge", "agg": "sum"},
        "transit_avg": {"expr": transit, "agg": "avg"},
        "on_time_avg": {"expr": on_time, "agg": "avg"},
        "n_ship": {"sql": "shipment_id", "agg": "count_distinct"},
        "picked_up_on": {"sql": "start", "agg": "max"},
        "delivered_on": {"sql": "end", "agg": "max"},
        "recent": {
            "over": {
                "fn": "last_n",
                "of": "net_charge",
                "n": 2,
                "partition_by": ["carrier_id"],
                "order_by": ["start", "shipment_id"],
            },
            "agg": "last",
        },
        "running_net": {
            "over": {
                "fn": "running_sum",
                "of": "net_charge",
                "partition_by": ["carrier_id"],
                "order_by": ["start", "shipment_id"],
            },
            "agg": "max",
        },
    }


def _freight_kpi(kpi_id: int, *, having: bool = False) -> dict:
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
            {"name": "lane", "from": "lane"},
            {"name": "carrier_id", "from": "carrier_id"},
            {"name": "shipment_id", "from": "shipment_id"},
        ],
        "default_dimensions": ["region", "lane"],
        "identity_grain": ["carrier_id", "shipment_id"],
        "base_measures": _base_measures(),
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "transit_days_now": {"of": "transit_days", "op": "point", "offset": {"months": 0}},
            "late_days_now": {"of": "late_days", "op": "point", "offset": {"months": 0}},
            "net_now": {"of": "net_charge", "op": "point", "offset": {"months": 0}},
            "on_time_now": {"of": "on_time_flag", "op": "point", "offset": {"months": 0}},
            "recent_charges": {"of": "recent", "op": "point", "offset": {"months": 0}},
            "running_billed": {"of": "running_net", "op": "point", "offset": {"months": 0}},
            "picked_up_on": {"of": "picked_up_on", "op": "point", "offset": {"months": 0}},
            "delivered_on": {"of": "delivered_on", "op": "point", "offset": {"months": 0}},
            "review_days": {"op": "constant", "value": REVIEW_DAYS},
            "next_review": {
                "op": "fn",
                "fn": "date_add",
                "inputs": ["delivered_on", "review_days"],
                "params": {"unit": "day"},
            },
            "transit_check": {
                "op": "expr",
                "expr": "date_diff(picked_up_on, delivered_on, 'day')",
            },
            "epoch_delivered": {
                "op": "fn",
                "fn": "epoch_day",
                "inputs": ["delivered_on"],
            },
            "total_charge": {"of": "billed", "op": "point", "offset": {"months": 0}},
            "charge_2m": {
                "of": "billed",
                "op": "window",
                "trailing": {"months": 2},
                "inclusive": True,
            },
            "avg_transit": {"of": "transit_avg", "op": "point", "offset": {"months": 0}},
            "on_time_rate": {"of": "on_time_avg", "op": "point", "offset": {"months": 0}},
            "n_shipments": {"of": "n_ship", "op": "point", "offset": {"months": 0}},
            "charge_rank": {
                "op": "rank",
                "of": "total_charge",
                "order": "desc",
                "cuts": ["G"],
            },
            "group_share": {
                "op": "percent_of_total",
                "of": "total_charge",
                "cuts": ["G"],
            },
            "sla_ok": {
                "op": "predicate",
                "match": "all",
                "predicates": [{"of": "on_time_rate", "cmp": "gte", "value": 0.5}],
            },
            "healthy": {
                "op": "predicate",
                "match": "all",
                "predicates": [
                    {"of": "on_time_rate", "cmp": "gte", "value": 0.5},
                    {"of": "total_charge", "cmp": "gt", "value": 40},
                ],
            },
        },
        "green_when": {"of": "total_charge", "above": 50},
    }
    if having:
        spec["having"] = {
            "match": "all",
            "predicates": [{"of": "total_charge", "cmp": "gt", "value": 0}],
        }
    return spec


def _claims_kpi(kpi_id: int) -> dict:
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "dimensions": [
            {"name": "region", "from": "region"},
            {"name": "claim_id", "from": "claim_id"},
        ],
        "default_dimensions": ["region"],
        "base_measures": {"open_amount": {"sql": "amount", "agg": "sum"}},
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "open_balance": {"of": "open_amount", "op": "point", "offset": {"months": 0}},
        },
    }


def _write_freight(extra_config, kpi_id: int, *, having: bool = False) -> None:
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", _freight_kpi(kpi_id, having=having))


def _context(
    path,
    extra_config,
    kpi_id,
    measures,
    *,
    selected_dimensions=None,
    having=False,
    month="2026-03",
):
    _write_freight(extra_config, kpi_id, having=having)
    ctx = make_context(
        path,
        measures=measures,
        kpi_id=kpi_id,
        month=month,
        selected_dimensions=selected_dimensions,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(SHIP_COLUMNS)
    return ctx


def _pick(result: dict, **dims) -> dict:
    matches = [
        row
        for row in result["rows"]
        if row.get("output_cut") == "G"
        and all(row.get(name) == value for name, value in dims.items())
    ]
    assert matches, (dims, result["rows"])
    assert len(matches) == 1, (dims, matches)
    return matches[0]


def _iso(value) -> str:
    text = str(value)
    return text[:10]


def _approx_list(actual, expected) -> None:
    actual = unwrap_cell(actual)
    assert actual is not None, expected
    assert len(actual) == len(expected), (actual, expected)
    for got, want in zip(actual, expected):
        assert got == pytest.approx(want), (actual, expected)


def test_e2e_freight_identity_helpers_dates_last_n(tmp_path, extra_config):
    """Per-shipment SLA: helper as of, last_n lists, date_add ISO, date_diff, epoch."""
    ships = pd.DataFrame(_shipments())
    path = tmp_path / "ships.parquet"
    ships.to_parquet(path, index=False)
    ctx = _context(
        path,
        extra_config,
        9970,
        IDENTITY_MEASURES,
        selected_dimensions=["carrier_id", "shipment_id"],
    )
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"] == result["sql"]
    assert result["selected_dimensions"] == ["carrier_id", "shipment_id"]

    expected = _identity_oracle(ships)
    assert len(result["rows"]) == 5
    for shipment_id, exp in expected.items():
        row = _pick(result, shipment_id=shipment_id, carrier_id=exp["carrier_id"])
        assert row["transit_days_now"] == pytest.approx(exp["transit_days"])
        assert row["late_days_now"] == pytest.approx(exp["late_days"])
        assert row["net_now"] == pytest.approx(exp["net_charge"])
        assert row["on_time_now"] == pytest.approx(exp["on_time"])
        assert row["running_billed"] == pytest.approx(exp["running_billed"])
        _approx_list(row["recent_charges"], exp["recent_charges"])
        assert _iso(row["delivered_on"]) == exp["delivered_iso"]
        assert row["next_review"] == exp["next_review"]
        assert row["transit_check"] == pytest.approx(exp["transit_days"])
        assert row["epoch_delivered"] == pytest.approx(exp["epoch_delivered"])

    late = _pick(result, shipment_id="SHP-2")
    assert late["late_days_now"] == pytest.approx(2.0)
    assert late["on_time_now"] == pytest.approx(0.0)
    assert late["next_review"] == "2026-03-13"
    _approx_list(late["recent_charges"], [44.8, 14.8])

    on_time = _pick(result, shipment_id="SHP-1")
    assert on_time["transit_days_now"] == pytest.approx(2.0)
    assert on_time["next_review"] == "2026-03-10"
    _approx_list(on_time["recent_charges"], [44.8])

    running = _pick(result, shipment_id="SHP-4")
    assert running["running_billed"] == pytest.approx(44.8 + 14.8 + 47.2)
    _approx_list(running["recent_charges"], [14.8, 47.2])


def test_e2e_freight_settlement_rank_having_fill_zero(tmp_path, extra_config):
    """Region × lane billed totals; Feb-only Economy densifies to 0 and is dropped."""
    ships = pd.DataFrame(_shipments())
    path = tmp_path / "ships.parquet"
    ships.to_parquet(path, index=False)
    ctx = _context(
        path,
        extra_config,
        9971,
        SETTLEMENT_MEASURES,
        selected_dimensions=["region", "lane"],
        having=True,
    )
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"] == result["sql"]

    expected = _group_oracle(ships)
    for exp in expected:
        exp["charge_2m"] = _charge_2m(ships, exp["region"], exp["lane"])

    lanes = {(row["region"], row["lane"]) for row in result["rows"]}
    assert ("NA", "Economy") not in lanes
    assert any(item["reason"] == "having" for item in result["dropped_groups"])
    assert len(result["rows"]) == 4

    for exp in expected:
        row = _pick(result, region=exp["region"], lane=exp["lane"])
        assert row["total_charge"] == pytest.approx(exp["total_charge"])
        assert row["charge_2m"] == pytest.approx(exp["charge_2m"])
        assert row["avg_transit"] == pytest.approx(exp["avg_transit"])
        assert row["on_time_rate"] == pytest.approx(exp["on_time_rate"])
        assert row["n_shipments"] == pytest.approx(exp["n_shipments"])
        assert row["charge_rank"] == exp["charge_rank"]
        assert row["group_share"] == pytest.approx(exp["group_share"])
        assert row["sla_ok"] == pytest.approx(exp["sla_ok"])
        assert row["healthy"] == pytest.approx(exp["healthy"])
        want_green = exp["total_charge"] >= 50
        assert row["green"] is want_green

    na_std = _pick(result, region="NA", lane="Standard")
    assert value_of(na_std, "charge_rank") == 1
    assert na_std["green"] is True
    eu_std = _pick(result, region="EU", lane="Standard")
    assert eu_std["healthy"] == pytest.approx(0.0)
    assert eu_std["sla_ok"] == pytest.approx(0.0)
    assert eu_std["total_charge"] == pytest.approx(9.96)


def test_e2e_freight_helper_of_at_region_is_bind_error(tmp_path, extra_config):
    """Helpers as measures.of need the identity grain, not the settlement grain."""
    ships = pd.DataFrame(_shipments())
    path = tmp_path / "ships.parquet"
    ships.to_parquet(path, index=False)
    ctx = _context(
        path,
        extra_config,
        9972,
        ["net_now", "total_charge"],
        selected_dimensions=["region", "lane"],
    )
    with pytest.raises(BindError, match="identity_grain"):
        validate(ctx, config_dir=extra_config)
    with pytest.raises(BindError, match="identity_grain"):
        compute(ctx, config_dir=extra_config)


def test_e2e_open_claims_snapshot_keeps_reporting_month(tmp_path, extra_config):
    """AR-style snapshot: no time column; host reporting_month is skipped as no_time."""
    claims = pd.DataFrame(
        [
            {"region": "NA", "claim_id": "CL-1", "amount": 100.0},
            {"region": "NA", "claim_id": "CL-2", "amount": 50.0},
            {"region": "EU", "claim_id": "CL-3", "amount": 80.0},
        ]
    )
    path = tmp_path / "claims.parquet"
    claims.to_parquet(path, index=False)
    write_yaml(extra_config / "kpis" / "9973.yaml", _claims_kpi(9973))
    ctx = make_context(
        path,
        measures=["open_balance"],
        kpi_id=9973,
        month="2026-03",
        selected_dimensions=["region"],
    )
    ctx["datasets"]["Sotif"]["columns"] = ["region", "claim_id", "amount"]
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sql"] == result["sql"]
    assert "date_trunc" not in result["sql"].lower()
    assert result["parameters"]["anchor"] is None
    assert any(
        item.get("filter_code") == "reporting_month" and item.get("reason") == "no_time"
        for item in result["skipped_filters"]
    )
    na = _pick(result, region="NA")
    eu = _pick(result, region="EU")
    assert na["open_balance"] == pytest.approx(150.0)
    assert eu["open_balance"] == pytest.approx(80.0)
    assert ctx["filters"]["reporting_month"]["value"] == ["2026-03"]
