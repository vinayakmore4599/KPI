"""End-to-end: a real SaaS subscription KPI pack, easiest to hardest.

What this file provides
    One subscription-line fact (account × product × month) and one KPI YAML that
    a real ARR dashboard would ship, graded so each test adds one layer:
      L1  volume      — MRR, seats, distinct accounts, billable lines
      L2  ratios      — ARPA, MRR per seat (null-safe divide, not raw `/`)
      L3  period      — prior month, prior year, MoM, YoY, YTD, QTD
      L4  MRR bridge  — new / expansion / contraction / churn, NRR, GRR, quick ratio
      L5  cut ranking — plan share, rank, Pareto, ABC ntile, gap to leader
      L6  series      — 12m average, volatility, slope, hit rate, longest streak
      L7  governance  — target attainment, threshold, predicate, green_when, having

    Every number is asserted twice: against a pandas oracle built from the raw
    frame, and against hand-computed constants for 2026-03 so a wrong oracle
    cannot pass. No calculation is pushed into the model SQL — the model is a
    plain physical extract and all math lives in base_measures / measures.

Where it is used
    pytest tests/test_e2e_saas_subscription_kpis.py

When to use
    Add a case here when subscription-style KPIs (retention, expansion, cohort
    ranking) change. Add a *new* fact file instead when the grain changes.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.dates import month_range_inclusive
from tests.conftest import make_context, write_yaml

ANCHOR = date(2026, 3, 1)
MONTHS = month_range_inclusive(date(2025, 1, 1), ANCHOR)
LAST = len(MONTHS) - 1

# Seasonality on the Core product so the L6 series measures have real signal.
WOBBLE = (0, 0, 50, 0, -30, 0, 0, -80, 0, 40, 0, 0, 60, 0, 20)

# line_id, account, region, plan, product, base, step, seats, wobbly, first, last
LINES = (
    ("ACC-1-CORE", "ACC-1", "NA", "Enterprise", "Core", 5000, 100, 50, True, 0, LAST),
    ("ACC-1-ANLY", "ACC-1", "NA", "Enterprise", "Analytics", 1500, 50, 12, False, 0, LAST),
    ("ACC-2-CORE", "ACC-2", "NA", "Pro", "Core", 1200, 20, 20, True, 0, LAST),
    ("ACC-3-CORE", "ACC-3", "EU", "Enterprise", "Core", 4000, 80, 40, True, 0, LAST),
    ("ACC-3-ANLY", "ACC-3", "EU", "Enterprise", "Analytics", 1000, 40, 10, False, 0, LAST),
    ("ACC-4-CORE", "ACC-4", "EU", "Starter", "Core", 300, 5, 5, True, 0, LAST),
    ("ACC-5-CORE", "ACC-5", "NA", "Starter", "Core", 250, 5, 4, True, 0, LAST - 1),
    ("ACC-6-CORE", "ACC-6", "NA", "Pro", "Core", 900, 0, 10, False, LAST, LAST),
)

# A negotiated downgrade, so the anchor month has contraction as well as expansion.
ADJUST = {("ACC-2-CORE", LAST): -300.0}

FACT_COLUMNS = [
    "event_month",
    "region",
    "plan",
    "account_id",
    "product",
    "line_id",
    "mrr",
    "seats",
    "new_mrr",
    "expansion_mrr",
    "contraction_mrr",
    "churned_mrr",
    "support_tickets",
]

MRR_BAR = 16000.0
MRR_TARGET = 18000.0

L1_MEASURES = ["mrr", "accounts", "seats", "lines", "records", "billable_lines"]
L2_MEASURES = ["arpa", "mrr_per_seat", "tickets_per_account"]
L3_MEASURES = [
    "mrr",
    "mrr_prior_month",
    "mrr_prior_year",
    "mom_growth",
    "yoy_growth",
    "mrr_ytd",
    "mrr_qtd",
]
L4_MEASURES = [
    "mrr",
    "mrr_prior_month",
    "new_mrr",
    "expansion_mrr",
    "contraction_mrr",
    "churned_mrr",
    "net_new_mrr",
    "closing_mrr_check",
    "nrr",
    "grr",
    "gross_churn_rate",
    "quick_ratio",
]
L5_MEASURES = ["mrr", "plan_share", "plan_rank", "pareto_share", "abc_tier", "gap_to_best"]
L6_MEASURES = [
    "mrr",
    "mrr_12m_avg",
    "mrr_12m_volatility",
    "mrr_12m_slope",
    "months_above_bar",
    "longest_run_above_bar",
]
L7_MEASURES = ["mrr", "nrr", "mrr_target", "target_attainment", "beats_target", "at_risk"]


# --------------------------------------------------------------------------
# fact table
# --------------------------------------------------------------------------


def _mrr(base: int, step: int, idx: int, wobbly: bool, line_id: str) -> float:
    """Contracted MRR for one line in one month."""
    value = float(base + step * idx + (WOBBLE[idx] if wobbly else 0))
    return value + ADJUST.get((line_id, idx), 0.0)


def _rows() -> list[dict]:
    """Account-month subscription lines with a self-consistent movement bridge.

    Movements are derived from the contracted series, so for every month
    closing = opening + new + expansion - contraction - churn holds exactly.
    """
    out: list[dict] = []
    for line_id, account, region, plan, product, base, step, seats, wobbly, first, last in LINES:
        previous: float | None = None
        for idx in range(first, last + 1):
            value = _mrr(base, step, idx, wobbly, line_id)
            delta = 0.0 if previous is None else value - previous
            out.append(
                {
                    "event_month": MONTHS[idx],
                    "region": region,
                    "plan": plan,
                    "account_id": account,
                    "product": product,
                    "line_id": line_id,
                    "mrr": value,
                    "seats": float(seats),
                    "new_mrr": value if previous is None else 0.0,
                    "expansion_mrr": max(delta, 0.0),
                    "contraction_mrr": max(-delta, 0.0),
                    "churned_mrr": 0.0,
                    "support_tickets": float(idx % 4),
                }
            )
            previous = value
        if last < LAST:
            out.append(
                {
                    "event_month": MONTHS[last + 1],
                    "region": region,
                    "plan": plan,
                    "account_id": account,
                    "product": product,
                    "line_id": line_id,
                    "mrr": 0.0,
                    "seats": 0.0,
                    "new_mrr": 0.0,
                    "expansion_mrr": 0.0,
                    "contraction_mrr": 0.0,
                    "churned_mrr": float(previous or 0.0),
                    "support_tickets": 0.0,
                }
            )
    return out


def _frame() -> pd.DataFrame:
    frame = pd.DataFrame(_rows())
    return frame[FACT_COLUMNS]


def _parquet(tmp_path) -> str:
    path = tmp_path / "subscription_lines.parquet"
    _frame().to_parquet(path, index=False)
    return path


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------


def _month_frame(frame: pd.DataFrame, month: date) -> pd.DataFrame:
    stamped = pd.to_datetime(frame["event_month"]).dt.date
    return frame[stamped == month]


def _totals(chunk: pd.DataFrame) -> dict[str, float]:
    """Raw aggregates for one grain cell in one month."""
    return {
        "mrr": float(chunk["mrr"].sum()),
        "seats": float(chunk["seats"].sum()),
        "accounts": float(chunk["account_id"].nunique()),
        "lines": float(chunk["line_id"].nunique()),
        "records": float(chunk["mrr"].notna().sum()),
        "billable_lines": float((chunk["mrr"] > 0).sum()),
        "tickets": float(chunk["support_tickets"].sum()),
        "new_mrr": float(chunk["new_mrr"].sum()),
        "expansion_mrr": float(chunk["expansion_mrr"].sum()),
        "contraction_mrr": float(chunk["contraction_mrr"].sum()),
        "churned_mrr": float(chunk["churned_mrr"].sum()),
    }


def _by_grain(frame: pd.DataFrame, month: date, keys: tuple[str, ...]) -> dict[tuple, dict]:
    """Oracle aggregates keyed by the grain tuple, for one month."""
    chunk = _month_frame(frame, month)
    if not keys:
        return {(): _totals(chunk)}
    out: dict[tuple, dict] = {}
    for name, part in chunk.groupby(list(keys), dropna=False):
        key = name if isinstance(name, tuple) else (name,)
        out[key] = _totals(part)
    return out


def _series(frame: pd.DataFrame, keys: tuple[str, ...], months: list[date]) -> dict[tuple, list[float]]:
    """Per-grain monthly MRR across `months`, months with no rows omitted."""
    out: dict[tuple, list[float]] = {}
    for month in months:
        for key, totals in _by_grain(frame, month, keys).items():
            out.setdefault(key, []).append(totals["mrr"])
    return out


def _stdev(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _slope(values: list[float]) -> float:
    n = len(values)
    xs = list(range(n))
    sum_x, sum_y = float(sum(xs)), float(sum(values))
    sum_xx = float(sum(x * x for x in xs))
    sum_xy = float(sum(x * y for x, y in zip(xs, values)))
    return (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x)


def _longest_run(values: list[float], bar: float) -> float:
    best = run = 0
    for value in values:
        run = run + 1 if value >= bar else 0
        best = max(best, run)
    return float(best)


# --------------------------------------------------------------------------
# KPI YAML
# --------------------------------------------------------------------------


def _base_measures() -> dict:
    return {
        "mrr_amount": {"sql": "mrr", "agg": "sum"},
        "seat_count": {"sql": "seats", "agg": "sum"},
        "account_ids": {"sql": "account_id", "agg": "count_distinct"},
        "line_ids": {"sql": "line_id", "agg": "count_distinct"},
        "billed_rows": {"sql": "mrr", "agg": "count"},
        "billable_flag": {
            "sql": "line_id",
            "agg": "count",
            "where": {"column": "mrr", "op": "gt", "value": 0},
        },
        "ticket_count": {"sql": "support_tickets", "agg": "sum"},
        "new_amount": {"sql": "new_mrr", "agg": "sum"},
        "expansion_amount": {"sql": "expansion_mrr", "agg": "sum"},
        "contraction_amount": {"sql": "contraction_mrr", "agg": "sum"},
        "churn_amount": {"sql": "churned_mrr", "agg": "sum"},
    }


def _measures() -> dict:
    now = {"months": 0}
    return {
        # L1 volume
        "mrr": {"of": "mrr_amount", "op": "point", "offset": now},
        "seats": {"of": "seat_count", "op": "point", "offset": now},
        "accounts": {"of": "account_ids", "op": "point", "offset": now},
        "lines": {"of": "line_ids", "op": "point", "offset": now},
        "records": {"of": "billed_rows", "op": "point", "offset": now},
        "billable_lines": {"of": "billable_flag", "op": "point", "offset": now},
        "tickets": {"of": "ticket_count", "op": "point", "offset": now},
        # L2 ratios
        "arpa": {"op": "fn", "fn": "divide", "inputs": ["mrr", "accounts"]},
        "mrr_per_seat": {"op": "fn", "fn": "divide", "inputs": ["mrr", "seats"]},
        "tickets_per_account": {"op": "fn", "fn": "divide", "inputs": ["tickets", "accounts"]},
        # L3 period
        "mrr_prior_month": {"of": "mrr_amount", "op": "point", "offset": {"months": 1}},
        "mrr_prior_year": {"of": "mrr_amount", "op": "point", "offset": {"years": 1}},
        "mom_growth": {"op": "pct_change", "of": "mrr", "offset": {"months": 1}},
        "yoy_growth": {"op": "fn", "fn": "growth_pct", "inputs": ["mrr", "mrr_prior_year"]},
        "mrr_ytd": {"of": "mrr_amount", "op": "window", "range": "ytd"},
        "mrr_qtd": {"of": "mrr_amount", "op": "window", "range": "qtd"},
        # L4 MRR bridge and retention
        "new_mrr": {"of": "new_amount", "op": "point", "offset": now},
        "expansion_mrr": {"of": "expansion_amount", "op": "point", "offset": now},
        "contraction_mrr": {"of": "contraction_amount", "op": "point", "offset": now},
        "churned_mrr": {"of": "churn_amount", "op": "point", "offset": now},
        "net_new_mrr": {
            "op": "expr",
            "expr": "new_mrr + expansion_mrr - contraction_mrr - churned_mrr",
        },
        "closing_mrr_check": {"op": "expr", "expr": "mrr_prior_month + net_new_mrr"},
        "retained_mrr": {
            "op": "expr",
            "expr": "mrr_prior_month + expansion_mrr - contraction_mrr - churned_mrr",
        },
        "kept_mrr": {
            "op": "expr",
            "expr": "mrr_prior_month - contraction_mrr - churned_mrr",
        },
        "nrr": {"op": "fn", "fn": "attainment", "inputs": ["retained_mrr", "mrr_prior_month"]},
        "grr": {"op": "fn", "fn": "attainment", "inputs": ["kept_mrr", "mrr_prior_month"]},
        "gross_churn_rate": {
            "op": "fn",
            "fn": "attainment",
            "inputs": ["churned_mrr", "mrr_prior_month"],
        },
        "mrr_gained": {"op": "expr", "expr": "new_mrr + expansion_mrr"},
        "mrr_lost": {"op": "expr", "expr": "contraction_mrr + churned_mrr"},
        "quick_ratio": {"op": "fn", "fn": "divide", "inputs": ["mrr_gained", "mrr_lost"]},
        # L5 cut ranking
        "plan_share": {"op": "percent_of_total", "of": "mrr", "cuts": ["G"]},
        "plan_rank": {"op": "rank", "of": "mrr", "order": "desc", "cuts": ["G"]},
        "pareto_share": {"op": "cumulative_share", "of": "mrr", "order": "desc", "cuts": ["G"]},
        "abc_tier": {"op": "ntile", "of": "mrr", "tiles": 3, "order": "desc", "cuts": ["G"]},
        "gap_to_best": {"op": "gap_to_leader", "of": "mrr", "cuts": ["G"]},
        # L6 series
        "mrr_12m_avg": {
            "op": "hook",
            "hook": "period_avg",
            "of": "mrr_amount",
            "trailing": {"months": 12},
        },
        "mrr_12m_volatility": {
            "op": "hook",
            "hook": "period_stdev",
            "of": "mrr_amount",
            "trailing": {"months": 12},
        },
        "mrr_12m_slope": {
            "op": "hook",
            "hook": "slope",
            "of": "mrr_amount",
            "trailing": {"months": 12},
        },
        "months_above_bar": {
            "op": "hook",
            "hook": "hit_rate",
            "of": "mrr_amount",
            "trailing": {"months": 12},
            "value": MRR_BAR,
        },
        "longest_run_above_bar": {
            "op": "hook",
            "hook": "longest_streak",
            "of": "mrr_amount",
            "trailing": {"months": 12},
            "value": MRR_BAR,
        },
        # L7 governance
        "mrr_target": {"op": "constant", "value": MRR_TARGET},
        "target_attainment": {
            "op": "fn",
            "fn": "attainment",
            "inputs": ["mrr", "mrr_target"],
        },
        "beats_target": {"op": "threshold", "of": "mrr", "cmp": "gte", "vs": "mrr_target"},
        "at_risk": {
            "op": "predicate",
            "match": "any",
            "predicates": [
                {"of": "nrr", "cmp": "lt", "value": 100},
                {"of": "quick_ratio", "cmp": "lt", "value": 1},
            ],
        },
    }


def _saas_kpi(kpi_id: int, *, having: bool = False) -> dict:
    spec: dict = {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "subscription_line",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "region", "from": "region"},
            {"name": "plan", "from": "plan"},
            {"name": "product", "from": "product"},
            {"name": "account_id", "from": "account_id"},
        ],
        "default_dimensions": ["plan"],
        "base_measures": _base_measures(),
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": _measures(),
        "green_when": {"of": "nrr", "above": 100},
    }
    if having:
        spec["having"] = {
            "match": "all",
            "predicates": [{"of": "mrr", "cmp": "gt", "value": 1000}],
        }
    return spec


def _model() -> dict:
    return {
        "model_id": "subscription_line",
        "kind": "physical",
        "required_aliases": ["subscription_line"],
        "sources": {"subscription_line": {"alias": "subscription_line"}},
        "joins": [],
    }


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _setup(tmp_path, extra_config, kpi_id: int, measures: list[str], **kwargs):
    """Write the group-foldered KPI + model YAML and build a metadata context."""
    write_yaml(extra_config / "models" / "saas" / "subscription_line.yaml", _model())
    write_yaml(
        extra_config / "kpis" / "saas" / f"{kpi_id}.yaml",
        _saas_kpi(kpi_id, having=kwargs.pop("having", False)),
    )
    ctx = make_context(
        _parquet(tmp_path),
        measures=measures,
        kpi_id=kpi_id,
        month=kwargs.pop("month", "2026-03"),
        **kwargs,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(FACT_COLUMNS)
    ctx["datasets"]["Sotif"]["alias"] = "subscription_line"
    return ctx


def _run(ctx, extra_config) -> dict:
    """validate() and compute() must agree before any assertion is trusted."""
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True, planned
    assert planned["sql"] == result["sql"]
    return result


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


# --------------------------------------------------------------------------
# L1 — volume
# --------------------------------------------------------------------------


def test_saas_l1_volume_by_plan(tmp_path, extra_config):
    """Plain SUM / COUNT / COUNT DISTINCT plus a CASE row helper, by plan."""
    ctx = _setup(tmp_path, extra_config, 9601, L1_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    expected = _by_grain(_frame(), ANCHOR, ("plan",))
    assert len(result["rows"]) == len(expected) == 3
    for (plan,), want in expected.items():
        row = _pick(result, plan=plan)
        assert row["mrr"] == pytest.approx(want["mrr"])
        assert row["seats"] == pytest.approx(want["seats"])
        assert row["accounts"] == pytest.approx(want["accounts"])
        assert row["lines"] == pytest.approx(want["lines"])
        assert row["records"] == pytest.approx(want["records"])
        assert row["billable_lines"] == pytest.approx(want["billable_lines"])

    enterprise = _pick(result, plan="Enterprise")
    assert enterprise["mrr"] == pytest.approx(15320.0)
    assert enterprise["seats"] == pytest.approx(112.0)
    assert enterprise["accounts"] == pytest.approx(2.0)
    assert enterprise["lines"] == pytest.approx(4.0)
    assert enterprise["records"] == pytest.approx(4.0)

    starter = _pick(result, plan="Starter")
    assert starter["mrr"] == pytest.approx(390.0)
    assert starter["lines"] == pytest.approx(2.0)
    # ACC-5 churned: its zero-MRR closing row still counts as a line, not as billable.
    assert starter["billable_lines"] == pytest.approx(1.0)


def test_saas_l1_volume_by_region(tmp_path, extra_config):
    """The same KPI answers a different host grain without a YAML change."""
    ctx = _setup(tmp_path, extra_config, 9602, L1_MEASURES, selected_dimensions=["region"])
    result = _run(ctx, extra_config)

    expected = _by_grain(_frame(), ANCHOR, ("region",))
    for (region,), want in expected.items():
        row = _pick(result, region=region)
        assert row["mrr"] == pytest.approx(want["mrr"])
        assert row["accounts"] == pytest.approx(want["accounts"])

    assert _pick(result, region="NA")["mrr"] == pytest.approx(6420 + 2200 + 1200 + 0 + 900)
    assert _pick(result, region="EU")["mrr"] == pytest.approx(5140 + 1560 + 390)


# --------------------------------------------------------------------------
# L2 — ratios
# --------------------------------------------------------------------------


def test_saas_l2_ratio_kpis(tmp_path, extra_config):
    """ARPA and MRR per seat: ratio of two aggregates, not an average of ratios."""
    ctx = _setup(tmp_path, extra_config, 9603, L2_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    for (plan,), want in _by_grain(_frame(), ANCHOR, ("plan",)).items():
        row = _pick(result, plan=plan)
        assert row["arpa"] == pytest.approx(want["mrr"] / want["accounts"])
        assert row["mrr_per_seat"] == pytest.approx(want["mrr"] / want["seats"])
        assert row["tickets_per_account"] == pytest.approx(want["tickets"] / want["accounts"])

    enterprise = _pick(result, plan="Enterprise")
    assert enterprise["arpa"] == pytest.approx(15320.0 / 2.0)
    assert enterprise["mrr_per_seat"] == pytest.approx(15320.0 / 112.0)


def test_saas_l2_divide_by_zero_is_null_not_inf(tmp_path, extra_config):
    """ACC-5's closing row has zero seats and zero MRR, so per-seat MRR is null."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9604,
        L2_MEASURES,
        selected_dimensions=["account_id"],
    )
    result = _run(ctx, extra_config)

    expected = _by_grain(_frame(), ANCHOR, ("account_id",))
    zero_seat = [key for key, want in expected.items() if want["seats"] == 0]
    assert zero_seat == [("ACC-5",)], expected

    for (account,), want in expected.items():
        row = _pick(result, account_id=account)
        if want["seats"] == 0:
            assert row["mrr_per_seat"] is None
        else:
            assert row["mrr_per_seat"] == pytest.approx(want["mrr"] / want["seats"])
        # One account per group, so ARPA collapses onto that account's MRR.
        assert row["arpa"] == pytest.approx(want["mrr"])


# --------------------------------------------------------------------------
# L3 — period comparisons
# --------------------------------------------------------------------------


def test_saas_l3_period_kpis(tmp_path, extra_config):
    """Prior month / prior year points, MoM, YoY, YTD and QTD from one anchor."""
    ctx = _setup(tmp_path, extra_config, 9605, L3_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    frame = _frame()
    prior_month = _by_grain(frame, MONTHS[LAST - 1], ("plan",))
    prior_year = _by_grain(frame, date(2025, 3, 1), ("plan",))
    ytd_months = [m for m in MONTHS if m.year == ANCHOR.year and m <= ANCHOR]
    ytd = _series(frame, ("plan",), ytd_months)

    for (plan,), want in _by_grain(frame, ANCHOR, ("plan",)).items():
        row = _pick(result, plan=plan)
        previous = prior_month[(plan,)]["mrr"]
        last_year = prior_year.get((plan,), {"mrr": 0.0})["mrr"]
        assert row["mrr_prior_month"] == pytest.approx(previous)
        assert row["mrr_prior_year"] == pytest.approx(last_year)
        assert row["mom_growth"] == pytest.approx((want["mrr"] - previous) / previous)
        assert row["yoy_growth"] == pytest.approx((want["mrr"] - last_year) / last_year)
        assert row["mrr_ytd"] == pytest.approx(sum(ytd[(plan,)]))
        # Anchor is the last month of Q1, so QTD and YTD coincide here.
        assert row["mrr_qtd"] == pytest.approx(row["mrr_ytd"])

    pro = _pick(result, plan="Pro")
    # Pro opened at 1460 (ACC-2 only), downgraded 300 and gained a 900 logo.
    assert pro["mrr_prior_month"] == pytest.approx(1460.0)
    assert pro["mom_growth"] == pytest.approx((2100.0 - 1460.0) / 1460.0)
    assert pro["mrr_prior_year"] == pytest.approx(1290.0)
    assert pro["yoy_growth"] == pytest.approx((2100.0 - 1290.0) / 1290.0)


def test_saas_l3_total_growth_at_company_grain(tmp_path, extra_config):
    """Whole-book MoM and YoY, hand-checked against the contracted schedule."""
    ctx = _setup(tmp_path, extra_config, 9606, L3_MEASURES, selected_dimensions=[])
    result = _run(ctx, extra_config)

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["mrr"] == pytest.approx(17810.0)
    assert row["mrr_prior_month"] == pytest.approx(17150.0)
    assert row["mrr_prior_year"] == pytest.approx(14100.0)
    assert row["mom_growth"] == pytest.approx((17810.0 - 17150.0) / 17150.0)
    assert row["yoy_growth"] == pytest.approx((17810.0 - 14100.0) / 14100.0)
    assert row["mrr_ytd"] == pytest.approx(17150.0 + 17150.0 + 17810.0)


# --------------------------------------------------------------------------
# L4 — MRR bridge and retention
# --------------------------------------------------------------------------


def test_saas_l4_mrr_bridge_reconciles(tmp_path, extra_config):
    """closing = opening + new + expansion - contraction - churn, per plan and in total."""
    ctx = _setup(tmp_path, extra_config, 9607, L4_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    frame = _frame()
    prior = _by_grain(frame, MONTHS[LAST - 1], ("plan",))
    for (plan,), want in _by_grain(frame, ANCHOR, ("plan",)).items():
        row = _pick(result, plan=plan)
        opening = prior[(plan,)]["mrr"]
        net_new = (
            want["new_mrr"] + want["expansion_mrr"] - want["contraction_mrr"] - want["churned_mrr"]
        )
        assert row["new_mrr"] == pytest.approx(want["new_mrr"])
        assert row["expansion_mrr"] == pytest.approx(want["expansion_mrr"])
        assert row["contraction_mrr"] == pytest.approx(want["contraction_mrr"])
        assert row["churned_mrr"] == pytest.approx(want["churned_mrr"])
        assert row["net_new_mrr"] == pytest.approx(net_new)
        assert row["closing_mrr_check"] == pytest.approx(opening + net_new)
        assert row["closing_mrr_check"] == pytest.approx(row["mrr"])

    pro = _pick(result, plan="Pro")
    assert pro["new_mrr"] == pytest.approx(900.0)
    assert pro["contraction_mrr"] == pytest.approx(260.0)
    starter = _pick(result, plan="Starter")
    assert starter["churned_mrr"] == pytest.approx(315.0)


def test_saas_l4_retention_kpis(tmp_path, extra_config):
    """NRR, GRR, gross churn and quick ratio at the whole-book grain."""
    ctx = _setup(tmp_path, extra_config, 9608, L4_MEASURES, selected_dimensions=[])
    result = _run(ctx, extra_config)
    row = result["rows"][0]

    opening, expansion, contraction, churned, new = 17150.0, 335.0, 260.0, 315.0, 900.0
    assert row["mrr_prior_month"] == pytest.approx(opening)
    assert row["expansion_mrr"] == pytest.approx(expansion)
    assert row["contraction_mrr"] == pytest.approx(contraction)
    assert row["churned_mrr"] == pytest.approx(churned)
    assert row["new_mrr"] == pytest.approx(new)

    assert row["nrr"] == pytest.approx(
        (opening + expansion - contraction - churned) * 100.0 / opening
    )
    assert row["grr"] == pytest.approx((opening - contraction - churned) * 100.0 / opening)
    assert row["gross_churn_rate"] == pytest.approx(churned * 100.0 / opening)
    assert row["quick_ratio"] == pytest.approx(
        (new + expansion) / (contraction + churned)
    )
    # NRR below 100 means the book shrank on existing customers; new logos hid it.
    assert row["nrr"] < 100.0
    assert row["mrr"] > row["mrr_prior_month"]
    assert row["green"] is False


def test_saas_l4_retention_is_null_when_there_is_no_opening_book(tmp_path, extra_config):
    """ACC-6 signed at the anchor: no opening MRR, so NRR and GRR must be null."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9609,
        L4_MEASURES + ["accounts"],
        selected_dimensions=["account_id"],
    )
    result = _run(ctx, extra_config)

    newborn = _pick(result, account_id="ACC-6")
    assert newborn["new_mrr"] == pytest.approx(900.0)
    assert newborn["mrr_prior_month"] in (None, 0.0)
    assert newborn["nrr"] is None
    assert newborn["grr"] is None
    # Nothing was lost, so the quick ratio has a zero denominator and stays null.
    assert newborn["quick_ratio"] is None

    churner = _pick(result, account_id="ACC-5")
    assert churner["mrr"] == pytest.approx(0.0)
    assert churner["churned_mrr"] == pytest.approx(315.0)
    assert churner["gross_churn_rate"] == pytest.approx(100.0)
    assert churner["nrr"] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# L5 — ranking across the cut
# --------------------------------------------------------------------------


def test_saas_l5_plan_ranking_and_pareto(tmp_path, extra_config):
    """Share, RANK, Pareto, ABC tiles and gap-to-leader over the three plans."""
    ctx = _setup(tmp_path, extra_config, 9610, L5_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    totals = {plan: want["mrr"] for (plan,), want in _by_grain(_frame(), ANCHOR, ("plan",)).items()}
    grand = sum(totals.values())
    ordered = sorted(totals.items(), key=lambda item: -item[1])
    leader = ordered[0][1]

    running = 0.0
    for position, (plan, value) in enumerate(ordered, start=1):
        running += value
        row = _pick(result, plan=plan)
        assert row["mrr"] == pytest.approx(value)
        assert row["plan_share"] == pytest.approx(value * 100.0 / grand)
        assert row["plan_rank"] == position
        assert row["pareto_share"] == pytest.approx(running * 100.0 / grand)
        assert row["abc_tier"] == position
        assert row["gap_to_best"] == pytest.approx(value - leader)

    assert _pick(result, plan="Enterprise")["plan_share"] == pytest.approx(
        15320.0 * 100.0 / 17810.0
    )
    assert _pick(result, plan="Starter")["pareto_share"] == pytest.approx(100.0)
    assert _pick(result, plan="Enterprise")["gap_to_best"] == pytest.approx(0.0)


def test_saas_l5_ranking_repartitions_at_a_finer_grain(tmp_path, extra_config):
    """Six plan × product cells: RANK and Pareto follow the requested grain."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9611,
        L5_MEASURES,
        selected_dimensions=["plan", "product"],
    )
    result = _run(ctx, extra_config)

    totals = {
        key: want["mrr"] for key, want in _by_grain(_frame(), ANCHOR, ("plan", "product")).items()
    }
    grand = sum(totals.values())
    ordered = sorted(totals.items(), key=lambda item: -item[1])
    assert len(result["rows"]) == len(totals)

    for position, ((plan, product), value) in enumerate(ordered, start=1):
        row = _pick(result, plan=plan, product=product)
        assert row["plan_rank"] == position
        assert row["plan_share"] == pytest.approx(value * 100.0 / grand)

    top = ordered[0]
    assert top[0] == ("Enterprise", "Core")
    assert top[1] == pytest.approx(6420.0 + 5140.0)


# --------------------------------------------------------------------------
# L6 — trailing series behaviour
# --------------------------------------------------------------------------


def test_saas_l6_series_hooks(tmp_path, extra_config):
    """12-month mean, sample stdev, OLS slope, hit rate and longest run."""
    ctx = _setup(tmp_path, extra_config, 9612, L6_MEASURES, selected_dimensions=[])
    result = _run(ctx, extra_config)
    row = result["rows"][0]

    window = MONTHS[LAST - 11 : LAST + 1]
    assert len(window) == 12
    values = _series(_frame(), (), window)[()]

    assert row["mrr_12m_avg"] == pytest.approx(sum(values) / 12.0)
    assert row["mrr_12m_volatility"] == pytest.approx(_stdev(values))
    assert row["mrr_12m_slope"] == pytest.approx(_slope(values))
    assert row["months_above_bar"] == pytest.approx(
        sum(1 for v in values if v >= MRR_BAR) * 100.0 / 12.0
    )
    assert row["longest_run_above_bar"] == pytest.approx(_longest_run(values, MRR_BAR))
    # The book crosses 16k halfway through the window and never falls back,
    # so exactly half the months are hits and they form one unbroken run.
    assert row["months_above_bar"] == pytest.approx(50.0)
    assert row["longest_run_above_bar"] == pytest.approx(6.0)
    assert row["mrr_12m_slope"] > 0


def test_saas_l6_series_hooks_per_plan(tmp_path, extra_config):
    """Each plan gets its own trailing series; Starter never clears the bar."""
    ctx = _setup(tmp_path, extra_config, 9613, L6_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    window = MONTHS[LAST - 11 : LAST + 1]
    per_plan = _series(_frame(), ("plan",), window)
    for (plan,), values in per_plan.items():
        row = _pick(result, plan=plan)
        assert row["mrr_12m_avg"] == pytest.approx(sum(values) / len(values))
        assert row["mrr_12m_volatility"] == pytest.approx(_stdev(values))
        assert row["months_above_bar"] == pytest.approx(
            sum(1 for v in values if v >= MRR_BAR) * 100.0 / len(values)
        )

    assert _pick(result, plan="Starter")["months_above_bar"] == pytest.approx(0.0)
    assert _pick(result, plan="Starter")["longest_run_above_bar"] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# L7 — governance
# --------------------------------------------------------------------------


def test_saas_l7_targets_flags_and_green(tmp_path, extra_config):
    """Attainment vs a constant, threshold vs a measure, any-match predicate, green_when."""
    ctx = _setup(tmp_path, extra_config, 9614, L7_MEASURES, selected_dimensions=["plan"])
    result = _run(ctx, extra_config)

    for (plan,), want in _by_grain(_frame(), ANCHOR, ("plan",)).items():
        row = _pick(result, plan=plan)
        assert row["mrr_target"] == pytest.approx(MRR_TARGET)
        assert row["target_attainment"] == pytest.approx(want["mrr"] * 100.0 / MRR_TARGET)
        assert row["beats_target"] == pytest.approx(1.0 if want["mrr"] >= MRR_TARGET else 0.0)
        assert row["green"] is (row["nrr"] is not None and row["nrr"] >= 100)

    enterprise = _pick(result, plan="Enterprise")
    assert enterprise["target_attainment"] == pytest.approx(15320.0 * 100.0 / 18000.0)
    assert enterprise["beats_target"] == pytest.approx(0.0)
    # Enterprise only expanded (NRR 102), so it is the one green, not-at-risk plan.
    assert enterprise["nrr"] == pytest.approx((15010.0 + 310.0) * 100.0 / 15010.0)
    assert enterprise["at_risk"] == pytest.approx(0.0)
    assert enterprise["green"] is True

    starter = _pick(result, plan="Starter")
    assert starter["nrr"] == pytest.approx((680.0 + 25.0 - 315.0) * 100.0 / 680.0)
    assert starter["at_risk"] == pytest.approx(1.0)
    assert starter["green"] is False
    assert _pick(result, plan="Pro")["green"] is False


def test_saas_l7_having_drops_subscale_plans(tmp_path, extra_config):
    """A HAVING floor removes Starter from the payload and records why."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9615,
        L7_MEASURES,
        selected_dimensions=["plan"],
        having=True,
    )
    result = _run(ctx, extra_config)

    plans = {row["plan"] for row in result["rows"]}
    assert plans == {"Enterprise", "Pro"}
    assert any(item["reason"] == "having" for item in result["dropped_groups"])
    assert _pick(result, plan="Pro")["mrr"] == pytest.approx(2100.0)
