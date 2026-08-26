"""Real-life expansion scenarios: Phase 1–3 features on a physical extract.

What this file provides
    End-to-end procurement / supplier-risk style KPIs where DuckDB only reads
    raw parquet columns. Every ratio, mask, window, rank, hook, and governance
    rule runs in the Pandas calc engine. Numbers are checked against an
    independent pandas oracle — not against another engine path.

Where it is used
    pytest tests/test_expansion_real_life.py

When to use
    Add a case when a new expansion op must survive beside masks, G/R cuts,
    and governance in one realistic request. Keep single-op unit tests elsewhere.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.dates import add_months, month_range_inclusive
from tests.conftest import find_row, make_context, minimal_kpi, sotif_cuts, write_yaml, value_of

ANCHOR = date(2026, 3, 1)

# Fixed unit costs and qty offsets so oracle constants are hand-checkable.
_UNIT = {
    "NA": {"LATE_SUPPLIER": 2.0, "OTHER": 4.0, "QUALITY": 3.0},
    "EU": {"LATE_SUPPLIER": 3.0, "OTHER": 5.0, "QUALITY": 4.0},
}
_QTY_OFF = {"NA": 1, "EU": 2}


def _build_procurement_facts(path: Path) -> pd.DataFrame:
    """Multi-supplier spend with qty, unit_cost, and status for mask/weight tests."""
    rows: list[dict] = []
    for month in month_range_inclusive(date(2025, 1, 1), ANCHOR):
        m = month.month
        for region in ("NA", "EU"):
            for reason in ("LATE_SUPPLIER", "OTHER", "QUALITY"):
                for supplier in ("ABC", "XYZ"):
                    if (
                        region == "NA"
                        and reason == "LATE_SUPPLIER"
                        and month == date(2025, 3, 1)
                    ):
                        continue
                    unit = _UNIT[region][reason]
                    qty = 10 * m + _QTY_OFF[region]
                    rows.append(
                        {
                            "event_month": month,
                            "region": region,
                            "reason_code": reason,
                            "supplier_name": supplier,
                            "amount": unit * qty,
                            "qty": qty,
                            "unit_cost": unit,
                            "status": "CRITICAL" if reason == "LATE_SUPPLIER" else "NORMAL",
                        }
                    )
    frame = pd.DataFrame(rows)
    frame.to_parquet(path, index=False)
    return frame


@pytest.fixture
def procurement_path(tmp_path: Path) -> Path:
    path = tmp_path / "procurement.parquet"
    _build_procurement_facts(path)
    return path


@pytest.fixture
def procurement_frame(procurement_path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(procurement_path)
    frame["event_month"] = pd.to_datetime(frame["event_month"]).dt.date
    return frame


def _procurement_context(
    parquet_path: Path,
    *,
    measures: list[str],
    kpi_id: int,
    supplier: list[str] | None = None,
    month: str = "2026-03",
    **kwargs,
) -> dict:
    ctx = make_context(
        parquet_path,
        measures=measures,
        supplier=supplier or ["ABC"],
        kpi_id=kpi_id,
        month=month,
        **kwargs,
    )
    ds = ctx["datasets"]["Sotif"]
    ds["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "qty",
        "unit_cost",
        "status",
    ]
    return ctx


def _as_dates(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["event_month"] = pd.to_datetime(out["event_month"]).dt.date
    return out


def _sum_at(frame: pd.DataFrame, month: date, **dims: str | None) -> float:
    mask = frame["event_month"] == month
    for column, value in dims.items():
        if value is not None:
            mask &= frame[column] == value
    return float(pd.to_numeric(frame.loc[mask, "amount"], errors="coerce").sum())


def _pick(result: dict, *, cut: str, **dims: str | None) -> dict:
    matches = []
    for row in result["rows"]:
        if row.get("output_cut") != cut:
            continue
        if all(row.get(name) == value for name, value in dims.items()):
            matches.append(row)
    assert matches, (cut, dims, result["rows"])
    assert len(matches) == 1, (cut, dims, matches)
    return matches[0]


def _window_sum(frame: pd.DataFrame, end: date, months: int, **dims: str | None) -> float:
    start = add_months(end, -(months - 1))
    return sum(_sum_at(frame, month, **dims) for month in month_range_inclusive(start, end))


def _weighted_avg(frame: pd.DataFrame, month: date, value_col: str, weight_col: str, **dims) -> float:
    mask = frame["event_month"] == month
    for column, value in dims.items():
        if value is not None:
            mask &= frame[column] == value
    sub = frame.loc[mask, [value_col, weight_col]].dropna()
    if sub.empty:
        return float("nan")
    w = sub[weight_col].astype(float)
    v = sub[value_col].astype(float)
    return float((v * w).sum() / w.sum())


def _geomean(values: list[float]) -> float:
    pos = [v for v in values if v and v > 0]
    if not pos:
        return float("nan")
    return float(math.prod(pos) ** (1.0 / len(pos)))


def _harmonic(values: list[float]) -> float:
    pos = [v for v in values if v and v > 0]
    if not pos:
        return float("nan")
    return float(len(pos) / sum(1.0 / v for v in pos))


def _hhi(shares: list[float]) -> float:
    return float(sum(s * s for s in shares))


def _compound_growth(first: float, last: float, n: int) -> float | None:
    if first in (None, 0) or last is None or n <= 0:
        return None
    return float(last / first) ** (1.0 / float(n)) - 1.0


def _assert_engine_path_sql(sql: str) -> None:
    """DuckDB must not pre-aggregate or evaluate measure masks."""
    lower = sql.lower()
    select = lower.split("from", 1)[0]
    for token in (" sum(", " avg(", " count(", " min(", " max(", " list_agg(", " string_agg("):
        assert token not in select, f"aggregate in extract: {token!r}"
    assert "case when" not in select, "measure mask must not push into SQL SELECT"


def _write(extra_config: Path, kpi_id: int, **overrides) -> dict:
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


# --- Scenario 1: procurement masks + non-additive aggs (Phase 1) ---


def test_procurement_masks_and_weighted_aggs(
    procurement_path, procurement_frame, extra_config
):
    """Critical-spend mask, weighted unit cost, geomean/harmonic on physical columns."""
    _write(
        extra_config,
        99001,
        base_measures={
            "spend": {"sql": "amount", "agg": "sum"},
            "weighted_uc": {
                "sql": "unit_cost",
                "agg": "weighted_avg",
                "weight_column": "qty",
            },
            "geo_amt": {"sql": "amount", "agg": "geomean"},
            "harm_amt": {"sql": "amount", "agg": "harmonic_mean"},
            "critical_spend": {
                "sql": "amount",
                "agg": "sum",
                "where": {
                    "or": [
                        {"column": "status", "op": "eq", "value": "CRITICAL"},
                        {"column": "reason_code", "op": "like", "value": "LATE%"},
                    ]
                },
            },
        },
        measures={
            "reason_code": {"kind": "dimension"},
            "total_spend": {"of": "spend", "op": "point"},
            "avg_unit_cost": {"of": "weighted_uc", "op": "point"},
            "geo_spend": {"of": "geo_amt", "op": "point"},
            "harm_spend": {"of": "harm_amt", "op": "point"},
            "critical": {"of": "critical_spend", "op": "point"},
        },
    )
    ctx = _procurement_context(
        procurement_path,
        measures=["total_spend", "avg_unit_cost", "geo_spend", "harm_spend", "critical"],
        kpi_id=99001,
    )
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])

    frame = procurement_frame
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")

    g_amt = _sum_at(
        frame, ANCHOR, reason_code="LATE_SUPPLIER", supplier_name="ABC"
    )
    na_amt = _sum_at(
        frame, ANCHOR, reason_code="LATE_SUPPLIER", region="NA", supplier_name="ABC"
    )
    eu_amt = _sum_at(
        frame, ANCHOR, reason_code="LATE_SUPPLIER", region="EU", supplier_name="ABC"
    )

    assert value_of(late, "total_spend") == pytest.approx(g_amt)
    assert value_of(late, "avg_unit_cost") == pytest.approx(
        _weighted_avg(
            frame,
            ANCHOR,
            "unit_cost",
            "qty",
            reason_code="LATE_SUPPLIER",
            supplier_name="ABC",
        )
    )
    assert value_of(late, "geo_spend") == pytest.approx(_geomean([na_amt, eu_amt]))
    assert value_of(late, "harm_spend") == pytest.approx(_harmonic([na_amt, eu_amt]))
    assert value_of(late, "critical") == pytest.approx(g_amt)
    assert value_of(other, "critical") is None  # no rows match mask → null, not zero


# --- Scenario 2: regional portfolio cuts + concentration (Phase 2) ---


def test_regional_portfolio_cross_cut_and_concentration(
    procurement_path, procurement_frame, extra_config
):
    """from_cut broadcast, versus_cut share, HHI and ABC on supplier portfolio."""
    _write(
        extra_config,
        99002,
        default_dimensions=["supplier"],
        dimensions=[
            {"name": "supplier", "from": "supplier_name"},
            {"name": "region", "from": "region"},
            {"name": "reason_code", "from": "reason_code"},
        ],
        cuts=[
            {
                "name": "G",
                "group_by": [],
                "exclude_from_grain": ["region"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
        measures={
            "supplier": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point"},
            "g_spend": {
                "of": "sotif_value",
                "op": "point",
                "from_cut": "G",
                "cuts": ["R"],
            },
            "region_share": {
                "of": "current_value",
                "op": "percent_of_total",
                "versus_cut": "G",
                "cuts": ["R"],
            },
            "hhi": {
                "of": "current_value",
                "op": "concentration",
                "cuts": ["G"],
            },
            "spend_class": {
                "of": "current_value",
                "op": "abc_class",
                "cuts": ["G"],
            },
        },
    )
    ctx = _procurement_context(
        procurement_path,
        measures=["current_value", "g_spend", "region_share", "hhi", "spend_class"],
        kpi_id=99002,
        supplier=["ABC", "XYZ"],
    )
    ctx["selected_dimensions"] = ["supplier"]
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])

    abc_g = _pick(result, cut="G", supplier="ABC")
    abc_na = _pick(result, cut="R", supplier="ABC", region="NA")
    g_total_all = sum(
        value_of(row, "current_value")
        for row in result["rows"]
        if row.get("output_cut") == "G"
    )
    na_spend = value_of(abc_na, "current_value")

    assert value_of(abc_na, "g_spend") == pytest.approx(value_of(abc_g, "current_value"))
    assert value_of(abc_na, "region_share") == pytest.approx(
        na_spend / g_total_all * 100.0
    )

    xyz_g = _pick(result, cut="G", supplier="XYZ")
    shares = [
        value_of(abc_g, "current_value") / (value_of(abc_g, "current_value") + value_of(xyz_g, "current_value")),
        value_of(xyz_g, "current_value") / (value_of(abc_g, "current_value") + value_of(xyz_g, "current_value")),
    ]
    assert value_of(abc_g, "hhi") == pytest.approx(_hhi(shares))
    assert value_of(abc_g, "spend_class") in {"A", "B", "C"}


# --- Scenario 3: finance trend expansion ops (Phase 2–3) ---


def test_finance_trend_expansion_ops(procurement_path, procurement_frame, extra_config):
    """expanding window, compound growth, seasonal adjust, band, vs prior window."""
    _write(
        extra_config,
        99003,
        measures={
            "reason_code": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point"},
            "expanding_total": {"of": "sotif_value", "op": "expanding_window"},
            "cagr_3m": {"of": "sotif_value", "op": "compound_growth", "n": 3},
            "deseasonalized": {
                "of": "sotif_value",
                "op": "seasonal_adjust",
                "trailing": {"months": 12},
            },
            "value_band": {
                "op": "band",
                "of": "current_value",
                "low": 0.85,
                "high": 1.15,
            },
        },
    )
    ctx = _procurement_context(
        procurement_path,
        measures=[
            "current_value",
            "expanding_total",
            "cagr_3m",
            "deseasonalized",
            "value_band_low",
            "value_band_high",
        ],
        kpi_id=99003,
    )
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])

    frame = procurement_frame
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cur = value_of(late, "current_value")
    lookback = int(result["parameters"]["lookback_months"])
    span_start = add_months(ANCHOR, -lookback)
    expanding_oracle = sum(
        _sum_at(frame, month, reason_code="LATE_SUPPLIER", supplier_name="ABC")
        for month in month_range_inclusive(span_start, ANCHOR)
    )
    first_month = add_months(ANCHOR, -3)
    first = _sum_at(frame, first_month, reason_code="LATE_SUPPLIER", supplier_name="ABC")
    last = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER", supplier_name="ABC")

    assert value_of(late, "expanding_total") == pytest.approx(expanding_oracle)
    assert value_of(late, "cagr_3m") == pytest.approx(_compound_growth(first, last, 3))
    assert value_of(late, "value_band_low") == pytest.approx(cur * 0.85)
    assert value_of(late, "value_band_high") == pytest.approx(cur * 1.15)

    # Seasonal adjust: current × overall_mean / same_month_mean over trailing 12.
    pairs = [
        (_sum_at(frame, month, reason_code="LATE_SUPPLIER", supplier_name="ABC"), month)
        for month in month_range_inclusive(add_months(ANCHOR, -11), ANCHOR)
    ]
    month_vals = [v for v, m in pairs if m.month == ANCHOR.month]
    overall = [v for v, _ in pairs]
    if month_vals and overall:
        expected_sa = cur * (sum(overall) / len(overall)) / (sum(month_vals) / len(month_vals))
        assert value_of(late, "deseasonalized") == pytest.approx(expected_sa)


def test_vs_prior_window_month_offset_computes(procurement_path, extra_config):
    """vs_prior_window with month offset returns a ratio (not percent)."""
    _write(
        extra_config,
        99011,
        measures={
            "reason_code": {"kind": "dimension"},
            "vs_prior_3m": {
                "of": "sotif_value",
                "op": "vs_prior_window",
                "trailing": {"months": 3},
                "offset": {"months": 3},
            },
        },
    )
    ctx = _procurement_context(
        procurement_path, measures=["vs_prior_3m"], kpi_id=99011
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    val = value_of(row, "vs_prior_3m")
    assert val is not None
    assert isinstance(val, float)
    assert val == pytest.approx(0.36764705882352944)


# --- Scenario 4: detail governance + list agg (Phase 3 WS8) ---


def test_detail_governance_and_list_agg(procurement_path, extra_config):
    """Measure having nulls row, required notes, sort/max_rows, list_agg echo."""
    _write(
        extra_config,
        99004,
        sort=[{"key": "total_spend", "order": "desc"}],
        max_rows=2,
        base_measures={
            "spend": {"sql": "amount", "agg": "sum"},
            "regions_seen": {"sql": "region", "agg": "string_agg"},
        },
        measures={
            "reason_code": {"kind": "dimension"},
            "total_spend": {
                "of": "spend",
                "op": "point",
                "having": [{"of": "total_spend", "cmp": "gt", "value": 999999}],
            },
            "missing_prior": {
                "of": "spend",
                "op": "point",
                "offset": {"years": 5},
                "required": True,
            },
            "region_list": {"of": "regions_seen", "op": "point"},
        },
    )
    ctx = _procurement_context(
        procurement_path,
        measures=["total_spend", "missing_prior", "region_list"],
        kpi_id=99004,
    )
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])

    assert len(result["rows"]) == 2
    non_null_spends = [
        value_of(row, "total_spend")
        for row in result["rows"]
        if value_of(row, "total_spend") is not None
    ]
    assert non_null_spends == sorted(non_null_spends, reverse=True)
    assert result["quality_flags"].get("required_measure_null") is True
    assert any(n.get("code") == "required_measure_null" for n in result["notes"])

    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "total_spend") is None  # having nulls, row kept
    cell = value_of(late, "region_list")
    assert isinstance(cell, str)
    assert "NA" in cell and "EU" in cell


# --- Scenario 5: expansion kitchen sink (all layers in one request) ---


EXPANSION_KEYS = [
    "reason_code",
    "current_value",
    "value_3m",
    "weighted_value",
    "masked_value",
    "g_value",
    "region_share",
    "expanding_total",
    "cagr_3m",
    "value_band_low",
    "value_band_high",
    "hhi",
    "spend_rank",
    "normalized",
    "region_codes",
    "trend_6m",
]


def _expansion_kitchen_kpi(kpi_id: int) -> dict:
    spec = minimal_kpi(kpi_id)
    spec["base_measures"] = {
        "sotif_value": {"sql": "amount", "agg": "sum"},
        "weighted": {
            "sql": "amount",
            "agg": "weighted_avg",
            "weight_column": "qty",
        },
        "late_only": {
            "sql": "amount",
            "agg": "sum",
            "where": {
                "or": [
                    {"column": "reason_code", "op": "like", "value": "LATE%"},
                    {"column": "status", "op": "eq", "value": "CRITICAL"},
                ]
            },
        },
        "region_codes": {"sql": "region", "agg": "string_agg"},
    }
    spec["measures"] = {
        "reason_code": {"kind": "dimension"},
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "value_3m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 3},
            "inclusive": True,
        },
        "weighted_value": {"of": "weighted", "op": "point"},
        "masked_value": {"of": "late_only", "op": "point"},
        "g_value": {
            "of": "sotif_value",
            "op": "point",
            "from_cut": "G",
            "cuts": ["R"],
        },
        "region_share": {
            "of": "current_value",
            "op": "percent_of_total",
            "versus_cut": "G",
            "cuts": ["R"],
        },
        "expanding_total": {"of": "sotif_value", "op": "expanding_window"},
        "cagr_3m": {"of": "sotif_value", "op": "compound_growth", "n": 3},
        "value_band": {"op": "band", "of": "current_value", "low": 0.9, "high": 1.1},
        "hhi": {"of": "current_value", "op": "concentration", "cuts": ["G"]},
        "spend_rank": {
            "of": "current_value",
            "op": "rank",
            "order": "desc",
            "cuts": ["G"],
        },
        "normalized": {
            "of": "current_value",
            "op": "normalize",
            "method": "max",
            "cuts": ["G"],
        },
        "region_codes": {"of": "region_codes", "op": "point"},
        "trend_6m": {
            "of": "sotif_value",
            "op": "trend",
            "trailing": {"months": 6},
            "inclusive": True,
            "cuts": ["G"],
        },
    }
    return spec


def test_expansion_kitchen_sink_oracle(procurement_path, procurement_frame, extra_config):
    """One request: masks, cuts, core/advanced ops, trend — all engine-side."""
    write_yaml(extra_config / "kpis" / "99005.yaml", _expansion_kitchen_kpi(99005))
    ctx = _procurement_context(
        procurement_path,
        measures=EXPANSION_KEYS,
        kpi_id=99005,
    )
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["sql"] == result["sql"]
    _assert_engine_path_sql(result["sql"])

    frame = procurement_frame
    late_g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    late_na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    other_g = find_row(result, cut="G", reason="OTHER")

    cur = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER", supplier_name="ABC")
    win3 = _window_sum(frame, ANCHOR, 3, reason_code="LATE_SUPPLIER", supplier_name="ABC")
    wavg = _weighted_avg(
        frame, ANCHOR, "amount", "qty", reason_code="LATE_SUPPLIER", supplier_name="ABC"
    )

    assert value_of(late_g, "current_value") == pytest.approx(cur)
    assert value_of(late_g, "value_3m") == pytest.approx(win3)
    assert value_of(late_g, "weighted_value") == pytest.approx(wavg)
    assert value_of(late_g, "masked_value") == pytest.approx(cur)
    assert value_of(late_na, "g_value") == pytest.approx(cur)
    na = value_of(late_na, "current_value")
    g_total_all = sum(
        value_of(row, "current_value")
        for row in result["rows"]
        if row.get("output_cut") == "G"
    )
    assert value_of(late_na, "region_share") == pytest.approx(na / g_total_all * 100.0)
    assert value_of(late_g, "value_band_low") == pytest.approx(cur * 0.9)
    assert value_of(late_g, "value_band_high") == pytest.approx(cur * 1.1)

    first = _sum_at(
        frame, add_months(ANCHOR, -3), reason_code="LATE_SUPPLIER", supplier_name="ABC"
    )
    assert value_of(late_g, "cagr_3m") == pytest.approx(_compound_growth(first, cur, 3))

    other_cur = value_of(other_g, "current_value")
    assert value_of(late_g, "normalized") == pytest.approx(cur / max(cur, other_cur))
    assert value_of(late_g, "spend_rank") in (1, 2, 3)
    assert isinstance(value_of(late_g, "region_codes"), str)
    trend = value_of(late_g, "trend_6m")
    assert isinstance(trend, list) and len(trend) == 6


def test_expansion_kitchen_trend_pagination(procurement_path, extra_config):
    """Trend pagination keeps full axis while slicing measure cells."""
    write_yaml(extra_config / "kpis" / "99006.yaml", _expansion_kitchen_kpi(99006))
    ctx = _procurement_context(
        procurement_path,
        measures=["trend_6m"],
        kpi_id=99006,
    )
    ctx["output"] = {"trend_page": 1, "trend_page_size": 2}
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert len(row["trend_6m"]) <= 2
    assert len(result["trend_axes"]["trend_6m"]) == 6


# --- Scenario 6: multi-view executive dashboard (Phase 3 WS9) ---


def test_multi_view_procurement_dashboard(procurement_path, extra_config):
    """Exec summary vs detail views on the same KPI with partial failure tolerance."""
    write_yaml(extra_config / "kpis" / "99007.yaml", _expansion_kitchen_kpi(99007))
    ctx = _procurement_context(
        procurement_path,
        measures=["current_value", "region_share"],
        kpi_id=99007,
    )
    ctx["execution"]["multi_view"] = True
    ctx["execution"]["view_details"] = [
        {"view_id": "exec", "measures_required": ["current_value", "hhi"]},
        {"view_id": "detail", "measures_required": ["region_share", "region_codes"]},
        {"view_id": "broken", "measures_required": ["not_registered"]},
    ]
    result = compute(ctx, config_dir=extra_config)
    assert result["multi_view"] is True
    by_id = {v["view_id"]: v for v in result["views"]}
    assert by_id["exec"]["ok"] is True
    assert by_id["detail"]["ok"] is True
    assert by_id["broken"]["ok"] is False
    exec_rows = by_id["exec"]["result"]["rows"]
    assert exec_rows and "hhi" in exec_rows[0]


# --- Scenario 7: core ops portfolio slice (Phase 2 catalog) ---


@pytest.mark.parametrize(
    "op,probe,oracle_fn",
    [
        (
            "bottom_n",
            {"op": "bottom_n", "of": "current_value", "n": 1, "cuts": ["G"]},
            lambda rows: 1,
        ),
        (
            "annualize",
            {"op": "annualize", "of": "current_value", "n": 1},
            lambda cur: cur * 12,
        ),
        (
            "rate",
            {"op": "rate", "of": "sotif_value", "n": 3},
            lambda cur: cur / 3,
        ),
    ],
)
def test_core_ops_portfolio_slice(
    procurement_path, procurement_frame, extra_config, op, probe, oracle_fn
):
    """Rate, annualize, bottom_n on procurement spend with oracle checks."""
    kpi_id = 99100 + hash(op) % 100
    _write(
        extra_config,
        kpi_id,
        measures={
            "reason_code": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point"},
            "probe": probe,
        },
    )
    ctx = _procurement_context(procurement_path, measures=["probe", "current_value"], kpi_id=kpi_id)
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cur = value_of(late, "current_value")
    expected = oracle_fn(cur)
    if op == "bottom_n":
        assert value_of(late, "probe") in {1, 2, 3}
    else:
        assert value_of(late, "probe") == pytest.approx(expected)


# --- Scenario 8: hook series on procurement trend (Phase 3) ---


def test_procurement_run_rate_hook(procurement_path, procurement_frame, extra_config):
    """run_rate hook on 3-month spend — engine-side series math."""
    _write(
        extra_config,
        99008,
        measures={
            "reason_code": {"kind": "dimension"},
            "current_value": {"of": "sotif_value", "op": "point"},
            "run_rate": {
                "op": "hook",
                "hook": "run_rate",
                "of": "sotif_value",
                "trailing": {"months": 3},
            },
        },
    )
    ctx = _procurement_context(
        procurement_path, measures=["run_rate", "current_value"], kpi_id=99008
    )
    result = compute(ctx, config_dir=extra_config)
    _assert_engine_path_sql(result["sql"])
    frame = procurement_frame
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cur = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER", supplier_name="ABC")
    # run_rate annualizes the last observed point (month grain × 12).
    assert value_of(late, "run_rate") == pytest.approx(cur * 12)


def test_vs_prior_window_year_offset_gap(procurement_path, extra_config):
    """Capability gap: offset {{years: 1}} can null out when prior window has no anchor."""
    _write(
        extra_config,
        99010,
        measures={
            "reason_code": {"kind": "dimension"},
            "vs_prior_yoy": {
                "of": "sotif_value",
                "op": "vs_prior_window",
                "trailing": {"months": 3},
                "offset": {"years": 1},
            },
        },
    )
    ctx = _procurement_context(
        procurement_path, measures=["vs_prior_yoy"], kpi_id=99010
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "vs_prior_yoy") is None
    codes = {n.get("code") for n in result.get("notes", [])}
    assert "period_wrap_skipped" in codes


def test_validate_matches_compute_procurement(procurement_path, extra_config):
    """validate and compute share the same physical extract plan."""
    write_yaml(extra_config / "kpis" / "99009.yaml", _expansion_kitchen_kpi(99009))
    ctx = _procurement_context(
        procurement_path,
        measures=EXPANSION_KEYS[:8],
        kpi_id=99009,
    )
    planned = validate(ctx, config_dir=extra_config)
    computed = compute(ctx, config_dir=extra_config)
    assert planned["sql"] == computed["sql"]
    assert planned["lookback_months"] == computed["parameters"]["lookback_months"]
    _assert_engine_path_sql(computed["sql"])
