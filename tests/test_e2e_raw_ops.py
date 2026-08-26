"""End-to-end: raw line-level facts through DuckDB + Pandas, values vs an oracle.

What this file provides
    A shipment-style fact table (several events per month, nulls, zeros, status)
    and the shipped 3004 KPI against the Sotif fixture. Every measure is checked
    against an independent pandas sum of the same parquet.

Where it is used
    pytest tests/test_e2e_raw_ops.py.

When to use
    Add a grain here when a new everyday ops path (CASE, ratio, cut share) lands.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.dates import add_months, month_range_inclusive
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, unwrap_cell, value_of

ANCHOR = date(2026, 3, 1)
PRIOR = date(2025, 3, 1)
RAW_COLUMNS = [
    "event_month",
    "region",
    "reason_code",
    "supplier_name",
    "status",
    "amount",
    "ordered_qty",
    "shipped_qty",
    "on_time",
]


def _line(
    month: date,
    region: str,
    reason: str,
    supplier: str,
    status: str,
    amount: float | None,
    ordered: float,
    shipped: float,
    on_time: int,
) -> dict:
    """One raw shipment event."""
    return {
        "event_month": month,
        "region": region,
        "reason_code": reason,
        "supplier_name": supplier,
        "status": status,
        "amount": amount,
        "ordered_qty": ordered,
        "shipped_qty": shipped,
        "on_time": on_time,
    }


def _raw_facts() -> pd.DataFrame:
    """Line-level ops extract: ABC plus one XYZ row that a supplier filter drops."""
    return pd.DataFrame(
        [
            _line(ANCHOR, "NA", "LATE_SUPPLIER", "ABC", "O", 10.0, 5, 4, 1),
            _line(ANCHOR, "NA", "LATE_SUPPLIER", "ABC", "F", 20.0, 10, 10, 1),
            _line(ANCHOR, "NA", "LATE_SUPPLIER", "ABC", "O", None, 0, 0, 0),
            _line(ANCHOR, "EU", "LATE_SUPPLIER", "ABC", "O", 8.0, 8, 6, 0),
            _line(ANCHOR, "NA", "OTHER", "ABC", "F", 3.0, 3, 3, 1),
            _line(ANCHOR, "NA", "LATE_SUPPLIER", "XYZ", "O", 2.0, 2, 1, 1),
            _line(date(2026, 2, 1), "NA", "LATE_SUPPLIER", "ABC", "O", 15.0, 6, 6, 1),
            _line(date(2026, 2, 1), "EU", "LATE_SUPPLIER", "ABC", "F", 5.0, 5, 5, 1),
            _line(date(2026, 2, 1), "NA", "OTHER", "ABC", "O", 1.0, 1, 0, 0),
            _line(date(2026, 1, 1), "NA", "LATE_SUPPLIER", "ABC", "F", 12.0, 4, 4, 1),
            _line(date(2026, 1, 1), "EU", "LATE_SUPPLIER", "ABC", "O", 4.0, 4, 2, 0),
            _line(PRIOR, "NA", "LATE_SUPPLIER", "ABC", "O", 6.0, 3, 3, 1),
            _line(PRIOR, "EU", "LATE_SUPPLIER", "ABC", "F", 9.0, 9, 9, 1),
            _line(PRIOR, "NA", "OTHER", "ABC", "O", 2.0, 2, 2, 1),
        ]
    )


def _abc(frame: pd.DataFrame) -> pd.DataFrame:
    """Host supplier filter."""
    return frame[frame["supplier_name"] == "ABC"].copy()


def _filled(frame: pd.DataFrame) -> pd.Series:
    """CASE WHEN amount IS NULL THEN 0 ELSE amount END."""
    return pd.to_numeric(frame["amount"], errors="coerce").fillna(0)


def _open(frame: pd.DataFrame) -> pd.Series:
    """CASE WHEN status = 'O' THEN coalesce(amount, 0) ELSE 0 END."""
    amount = pd.to_numeric(frame["amount"], errors="coerce").fillna(0)
    return amount.where(frame["status"] == "O", 0)


def _sum_at(
    frame: pd.DataFrame,
    month: date,
    reason: str,
    region: str | None,
    series: pd.Series,
) -> float:
    """Oracle sum of `series` at one month / reason / optional region."""
    mask = (frame["event_month"] == month) & (frame["reason_code"] == reason)
    if region is not None:
        mask &= frame["region"] == region
    return float(series.loc[mask].sum())


def _window_sum(
    frame: pd.DataFrame,
    end: date,
    months: int,
    reason: str,
    region: str | None,
    series: pd.Series,
) -> float:
    """Inclusive trailing-N-month oracle."""
    start = add_months(end, -(months - 1))
    total = 0.0
    for month in month_range_inclusive(start, end):
        total += _sum_at(frame, month, reason, region, series)
    return total


def _approx(actual, expected) -> None:
    """Compare a computed measure to the oracle, including nulls."""
    actual = unwrap_cell(actual)
    if expected is None:
        assert actual is None or (isinstance(actual, float) and pd.isna(actual))
        return
    assert actual == pytest.approx(expected)


def _ops_kpi(kpi_id: int) -> dict:
    """Everyday ops KPI: CASE facts, ratio of totals, YoY, rank, share."""
    spec = minimal_kpi(kpi_id)
    spec["base_measures"] = {
        "filled_value": {
            "expr": "CASE WHEN amount IS NULL THEN 0 ELSE amount END",
            "agg": "sum",
        },
        "open_value": {
            "expr": "CASE WHEN status = 'O' THEN coalesce(amount, 0) ELSE 0 END",
            "agg": "sum",
        },
        "shipped_qty": {"sql": "shipped_qty", "agg": "sum"},
        "ordered_qty": {"sql": "ordered_qty", "agg": "sum"},
        "ontime_qty": {
            "columns": ["shipped_qty", "on_time"],
            "op": "multiply",
            "agg": "sum",
        },
    }
    spec["measures"] = {
        "current_value": {"of": "filled_value", "op": "point", "offset": {"months": 0}},
        "previous_year_value": {
            "of": "filled_value",
            "op": "point",
            "offset": {"years": 1},
        },
        "value_3m": {
            "of": "filled_value",
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
        "open_now": {"of": "open_value", "op": "point", "offset": {"months": 0}},
        "shipped_now": {"of": "shipped_qty", "op": "point", "offset": {"months": 0}},
        "ordered_now": {"of": "ordered_qty", "op": "point", "offset": {"months": 0}},
        "fill_rate": {
            "op": "fn",
            "fn": "percent",
            "inputs": ["shipped_now", "ordered_now"],
        },
        "ontime_now": {"of": "ontime_qty", "op": "point", "offset": {"months": 0}},
        "percent_gt": {"op": "percent_of_total", "of": "current_value", "cuts": ["G"]},
        "reason_rank": {"op": "rank", "of": "current_value", "order": "desc", "cuts": ["G"]},
    }
    return spec


def test_e2e_raw_line_facts_ops_dashboard(tmp_path, extra_config):
    """One request: extract raw lines, CASE/ratio/window/YoY/rank, match the oracle."""
    frame = _raw_facts()
    path = tmp_path / "ops.parquet"
    frame.to_parquet(path, index=False)
    work = _abc(frame)
    filled = _filled(work)
    opened = _open(work)

    write_yaml(extra_config / "kpis" / "9920.yaml", _ops_kpi(9920))
    measures = [
        "current_value",
        "previous_year_value",
        "value_3m",
        "yoy_month",
        "open_now",
        "fill_rate",
        "ontime_now",
        "percent_gt",
        "reason_rank",
    ]
    ctx = make_context(
        path,
        measures=measures,
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9920,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(RAW_COLUMNS)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert value_of(planned, "lookback_months") == 12
    assert "read_parquet" in planned["sql"].lower()
    assert '"event_month" IN' not in " ".join(planned["sql"].split())

    result = compute(ctx, config_dir=extra_config)
    assert result["sql"]
    for row in result["rows"]:
        for key in measures:
            if key in {"percent_gt", "reason_rank"} and row["output_cut"] != "G":
                continue
            assert key in row, (key, row)

    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    na_late = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")

    g_now = _sum_at(work, ANCHOR, "LATE_SUPPLIER", None, filled)
    g_ly = _sum_at(work, PRIOR, "LATE_SUPPLIER", None, filled)
    na_now = _sum_at(work, ANCHOR, "LATE_SUPPLIER", "NA", filled)
    other_now = _sum_at(work, ANCHOR, "OTHER", None, filled)

    _approx(g_late["current_value"], g_now)
    _approx(g_late["previous_year_value"], g_ly)
    _approx(g_late["value_3m"], _window_sum(work, ANCHOR, 3, "LATE_SUPPLIER", None, filled))
    _approx(g_late["yoy_month"], (g_now - g_ly) / g_ly)
    _approx(g_late["open_now"], _sum_at(work, ANCHOR, "LATE_SUPPLIER", None, opened))
    _approx(na_late["current_value"], na_now)
    _approx(na_late["open_now"], _sum_at(work, ANCHOR, "LATE_SUPPLIER", "NA", opened))
    _approx(g_other["current_value"], other_now)

    shipped_g = _sum_at(work, ANCHOR, "LATE_SUPPLIER", None, work["shipped_qty"])
    ordered_g = _sum_at(work, ANCHOR, "LATE_SUPPLIER", None, work["ordered_qty"])
    _approx(g_late["fill_rate"], shipped_g / ordered_g * 100)
    ontime_g = float(
        (work.loc[
            (work["event_month"] == ANCHOR) & (work["reason_code"] == "LATE_SUPPLIER"),
            "shipped_qty",
        ] * work.loc[
            (work["event_month"] == ANCHOR) & (work["reason_code"] == "LATE_SUPPLIER"),
            "on_time",
        ]).sum()
    )
    _approx(g_late["ontime_now"], ontime_g)

    total_g = g_now + other_now
    _approx(g_late["percent_gt"], g_now / total_g * 100)
    _approx(g_other["percent_gt"], other_now / total_g * 100)
    assert value_of(g_late, "reason_rank") == 1
    assert value_of(g_other, "reason_rank") == 2

    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}
    assert g_late["current_value"] != pytest.approx(na_late["current_value"])
    assert g_now == 38.0
    assert na_now == 30.0


def test_e2e_shipped_3004_all_measures_match_oracle(parquet_path, config_dir):
    """Shipped 3004.yaml: one request for every measure_key, numbers vs the fixture formula."""
    measures = [
        "reason_code",
        "current_value",
        "previous_year_value",
        "value_3m",
        "value_6m",
        "value_12m",
        "yoy_month",
        "trend_12m",
    ]
    ctx = make_context(parquet_path, measures=measures, supplier=["ABC"], region=["NA"])
    planned = validate(ctx, config_dir=config_dir)
    assert planned["ok"] is True
    assert value_of(planned, "lookback_months") == 12

    result = compute(ctx, config_dir=config_dir)
    frame = pd.read_parquet(parquet_path)
    frame["event_month"] = pd.to_datetime(frame["event_month"]).dt.date
    amount = pd.to_numeric(frame["amount"], errors="coerce")

    def at(month: date, reason: str, region: str | None) -> float | None:
        mask = (frame["event_month"] == month) & (frame["reason_code"] == reason)
        if region is not None:
            mask &= frame["region"] == region
        hits = amount.loc[mask]
        return None if hits.empty else float(hits.sum())

    def window(end: date, n: int, reason: str, region: str | None) -> float | None:
        start = add_months(end, -(n - 1))
        total = 0.0
        seen = False
        for month in month_range_inclusive(start, end):
            value = at(month, reason, region)
            if value is None:
                continue
            seen = True
            total += value
        return total if seen else None

    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")

    _approx(g["current_value"], at(ANCHOR, "LATE_SUPPLIER", None))
    _approx(g["previous_year_value"], at(PRIOR, "LATE_SUPPLIER", None))
    _approx(g["value_3m"], window(ANCHOR, 3, "LATE_SUPPLIER", None))
    _approx(g["value_6m"], window(ANCHOR, 6, "LATE_SUPPLIER", None))
    _approx(g["value_12m"], window(ANCHOR, 12, "LATE_SUPPLIER", None))
    current = at(ANCHOR, "LATE_SUPPLIER", None)
    prior = at(PRIOR, "LATE_SUPPLIER", None)
    _approx(g["yoy_month"], None if prior in (None, 0) else (current - prior) / prior)
    assert g["reason_code"] == "LATE_SUPPLIER"

    _approx(na["current_value"], at(ANCHOR, "LATE_SUPPLIER", "NA"))
    _approx(na["previous_year_value"], at(PRIOR, "LATE_SUPPLIER", "NA"))
    _approx(na["value_3m"], window(ANCHOR, 3, "LATE_SUPPLIER", "NA"))
    _approx(other["current_value"], at(ANCHOR, "OTHER", None))

    axis = [date.fromisoformat(item) for item in result["trend_axes"]["trend_12m"]]
    assert axis[0] == date(2025, 4, 1)
    assert axis[-1] == ANCHOR
    expected_trend = [window(month, 1, "LATE_SUPPLIER", None) or 0.0 for month in axis]
    assert g["trend_12m"] == pytest.approx(expected_trend)
    assert "trend_12m" not in na

    assert value_of(g, "current_value") == 45.0
    assert value_of(g, "previous_year_value") == 15.0
    assert value_of(g, "value_3m") == 90.0
    assert value_of(g, "value_6m") == 585.0
    assert value_of(g, "value_12m") == 1170.0
    assert value_of(g, "yoy_month") == 2.0
    assert value_of(na, "current_value") == 30.0
    assert na["previous_year_value"] is None
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}
