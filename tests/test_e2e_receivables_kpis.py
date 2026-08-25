"""End-to-end: accounts-receivable KPIs on a multi-currency open-invoice snapshot.

What this file provides
    A finance pack whose hard part is the row layer: every amount arrives in a
    local currency and has to be normalised, split into aging buckets and run
    through a provision matrix before anything can be aggregated.
      L1  FX          — lookup rate, then a four-step row chain to USD
      L2  aging       — five CASE buckets that must re-sum to the AR balance
      L3  DSO         — balance over billings times days in the period
      L4  IFRS 9      — expected credit loss from a per-bucket provision matrix
      L5  CEI         — collection effectiveness from last month's opening book
      L6  YoY         — index vs last year and which region drove the movement
      L7  risk        — top-N, quartiles and percent rank on overdue exposure

    The model YAML is a plain physical extract: no FX join, no bucket column,
    no provision table. Everything is derived by the KPI engine and checked
    against a pandas oracle plus hand-computed 2026-03 constants.

Where it is used
    pytest tests/test_e2e_receivables_kpis.py
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

FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.26, "INR": 0.012}

# IFRS 9 simplified provision matrix: loss rate by aging bucket.
PROVISION = {"current": 0.005, "b1_30": 0.02, "b31_60": 0.05, "b61_90": 0.15, "b90_plus": 0.40}
BUCKETS = ("current", "b1_30", "b31_60", "b61_90", "b90_plus")

DAYS_IN_PERIOD = 30.0

# customer, region, currency, segment, rating, base invoice value in local currency
CUSTOMERS = (
    ("CUST-1", "NA", "USD", "Enterprise", "A", 100000),
    ("CUST-2", "NA", "USD", "Mid-Market", "B", 60000),
    ("CUST-3", "EU", "EUR", "Enterprise", "A", 80000),
    ("CUST-4", "EU", "GBP", "Mid-Market", "C", 50000),
    ("CUST-5", "APAC", "INR", "SMB", "C", 4000000),
    ("CUST-6", "APAC", "USD", "SMB", "B", 30000),
)

# Days past due for the second and third open invoice, month by month.
DPD_MID = (5, 20, 40, 70, 100, 15, 35, 55, 95, 10, 25, 45, 75, 20, 50)
DPD_OLD = (95, 75, 55, 35, 15, 100, 80, 60, 40, 20, 5, 65, 30, 85, 100)
PAID_FRACTION = (0.60, 0.55, 0.65, 0.50, 0.70, 0.60, 0.45, 0.75, 0.55, 0.65, 0.60, 0.50, 0.70, 0.62, 0.58)

FACT_COLUMNS = [
    "event_month",
    "region",
    "segment",
    "currency",
    "customer_id",
    "credit_rating",
    "invoice_id",
    "invoice_amount",
    "paid_amount",
    "days_past_due",
]

L1_MEASURES = ["billed", "collected", "ar_balance", "invoices"]
L2_MEASURES = ["ar_balance", *[f"aging_{name}" for name in BUCKETS], "overdue_amount", "overdue_rate"]
L3_MEASURES = ["ar_balance", "billed", "dso", "overdue_rate"]
L4_MEASURES = ["ar_balance", "expected_credit_loss", "coverage_rate"]
L5_MEASURES = ["ar_balance", "opening_ar", "billed", "aging_current", "cei"]
L6_MEASURES = ["ar_balance", "ar_last_year", "ar_index", "yoy_contribution"]
L7_MEASURES = ["overdue_amount", "worst_offender", "risk_quartile", "overdue_percent_rank"]


# --------------------------------------------------------------------------
# open-invoice snapshot
# --------------------------------------------------------------------------


def _invoices(idx: int, base: int, phase: int) -> list[tuple[str, int, float, float]]:
    """Three open invoices per customer-month: current, mid-aged and old.

    `phase` staggers each customer along the aging cycle so a single month
    spans every bucket, which is what a real ledger looks like.
    """
    fraction = PAID_FRACTION[idx]
    slot = (idx + phase) % len(DPD_MID)
    return [
        ("CUR", 0, float(base), float(base) * fraction),
        ("MID", DPD_MID[slot], float(base) * 0.5, float(base) * 0.5 * 0.30),
        ("OLD", DPD_OLD[slot], float(base) * 0.25, 0.0),
    ]


def _rows() -> list[dict]:
    out: list[dict] = []
    for idx, month in enumerate(MONTHS):
        for phase, (customer, region, currency, segment, rating, base) in enumerate(CUSTOMERS):
            for tag, dpd, amount, paid in _invoices(idx, base, phase):
                out.append(
                    {
                        "event_month": month,
                        "region": region,
                        "segment": segment,
                        "currency": currency,
                        "customer_id": customer,
                        "credit_rating": rating,
                        "invoice_id": f"{customer}-{idx:02d}-{tag}",
                        "invoice_amount": amount,
                        "paid_amount": paid,
                        "days_past_due": float(dpd),
                    }
                )
    return out


def _frame() -> pd.DataFrame:
    """Raw frame plus the derived columns the engine is expected to reproduce."""
    frame = pd.DataFrame(_rows())[FACT_COLUMNS]
    rate = frame["currency"].map(FX)
    frame["billed_usd"] = frame["invoice_amount"] * rate
    frame["collected_usd"] = frame["paid_amount"] * rate
    frame["outstanding_usd"] = frame["billed_usd"] - frame["collected_usd"]
    frame["bucket"] = [_bucket(dpd) for dpd in frame["days_past_due"]]
    for name in BUCKETS:
        frame[f"aging_{name}"] = frame["outstanding_usd"].where(frame["bucket"] == name, 0.0)
    frame["provision_rate"] = frame["bucket"].map(PROVISION)
    frame["ecl_usd"] = frame["outstanding_usd"] * frame["provision_rate"]
    frame["overdue_usd"] = frame["outstanding_usd"].where(frame["days_past_due"] > 0, 0.0)
    return frame


def _bucket(dpd: float) -> str:
    if dpd <= 0:
        return "current"
    if dpd <= 30:
        return "b1_30"
    if dpd <= 60:
        return "b31_60"
    if dpd <= 90:
        return "b61_90"
    return "b90_plus"


def _parquet(tmp_path):
    path = tmp_path / "open_invoices.parquet"
    _frame()[FACT_COLUMNS].to_parquet(path, index=False)
    return path


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------

SUMMED = (
    "billed_usd",
    "collected_usd",
    "outstanding_usd",
    "ecl_usd",
    "overdue_usd",
    *[f"aging_{name}" for name in BUCKETS],
)


def _totals(chunk: pd.DataFrame) -> dict[str, float]:
    out = {name: float(chunk[name].sum()) for name in SUMMED}
    out["invoices"] = float(chunk["invoice_id"].nunique())
    return out


def _by_grain(month: date, keys: tuple[str, ...]) -> dict[tuple, dict]:
    frame = _frame()
    stamped = pd.to_datetime(frame["event_month"]).dt.date
    chunk = frame[stamped == month]
    if not keys:
        return {(): _totals(chunk)}
    out: dict[tuple, dict] = {}
    for name, part in chunk.groupby(list(keys), dropna=False):
        key = name if isinstance(name, tuple) else (name,)
        out[key] = _totals(part)
    return out


def _sql_ranks(values: list[float]) -> list[int]:
    """RANK() descending: ties share a rank and the next rank skips."""
    ordered = sorted(values, reverse=True)
    return [ordered.index(value) + 1 for value in values]


# --------------------------------------------------------------------------
# KPI YAML
# --------------------------------------------------------------------------


def _bucket_case(name: str, value: str) -> str:
    low = {"current": None, "b1_30": 0, "b31_60": 30, "b61_90": 60, "b90_plus": 90}[name]
    high = {"current": 0, "b1_30": 30, "b31_60": 60, "b61_90": 90, "b90_plus": None}[name]
    if low is None:
        test = f"days_past_due <= {high}"
    elif high is None:
        test = f"days_past_due > {low}"
    else:
        test = f"days_past_due > {low} AND days_past_due <= {high}"
    return f"CASE WHEN {test} THEN {value} ELSE 0 END"


def _provision_case() -> str:
    return (
        "CASE WHEN days_past_due <= 0 THEN 0.005 "
        "WHEN days_past_due <= 30 THEN 0.02 "
        "WHEN days_past_due <= 60 THEN 0.05 "
        "WHEN days_past_due <= 90 THEN 0.15 "
        "ELSE 0.40 END"
    )


def _base_measures() -> dict:
    """Row chain: FX rate -> USD amounts -> outstanding -> buckets -> provision."""
    measures: dict = {
        "fx_rate": {"lookup": {"column": "currency", "map": dict(FX), "default": 0}},
        "billed_usd": {"expr": "invoice_amount * fx_rate"},
        "collected_usd": {"expr": "paid_amount * fx_rate"},
        "outstanding_usd": {"expr": "billed_usd - collected_usd"},
        "provision_rate": {"expr": _provision_case()},
        "billed_total": {"expr": "billed_usd", "agg": "sum"},
        "collected_total": {"expr": "collected_usd", "agg": "sum"},
        "outstanding_total": {"expr": "outstanding_usd", "agg": "sum"},
        "overdue_total": {
            "expr": "CASE WHEN days_past_due > 0 THEN outstanding_usd ELSE 0 END",
            "agg": "sum",
        },
        "ecl_total": {"expr": "outstanding_usd * provision_rate", "agg": "sum"},
        "invoice_ids": {"sql": "invoice_id", "agg": "count_distinct"},
    }
    for name in BUCKETS:
        measures[f"bucket_{name}"] = {
            "expr": _bucket_case(name, "outstanding_usd"),
            "agg": "sum",
        }
    return measures


def _measures() -> dict:
    now = {"months": 0}
    measures: dict = {
        # L1
        "billed": {"of": "billed_total", "op": "point", "offset": now},
        "collected": {"of": "collected_total", "op": "point", "offset": now},
        "ar_balance": {"of": "outstanding_total", "op": "point", "offset": now},
        "invoices": {"of": "invoice_ids", "op": "point", "offset": now},
        # L2
        "overdue_amount": {"of": "overdue_total", "op": "point", "offset": now},
        "overdue_rate": {
            "op": "fn",
            "fn": "percent",
            "inputs": ["overdue_amount", "ar_balance"],
        },
        # L3
        "days_in_period": {"op": "constant", "value": DAYS_IN_PERIOD},
        "ar_over_billings": {"op": "fn", "fn": "divide", "inputs": ["ar_balance", "billed"]},
        "dso": {"op": "fn", "fn": "multiply", "inputs": ["ar_over_billings", "days_in_period"]},
        # L4
        "expected_credit_loss": {"of": "ecl_total", "op": "point", "offset": now},
        "coverage_rate": {
            "op": "fn",
            "fn": "percent",
            "inputs": ["expected_credit_loss", "ar_balance"],
        },
        # L5
        "opening_ar": {"of": "outstanding_total", "op": "point", "offset": {"months": 1}},
        "cei_numerator": {"op": "expr", "expr": "opening_ar + billed - ar_balance"},
        "cei_denominator": {"op": "expr", "expr": "opening_ar + billed - aging_current"},
        "cei": {"op": "fn", "fn": "attainment", "inputs": ["cei_numerator", "cei_denominator"]},
        # L6
        "ar_last_year": {"of": "outstanding_total", "op": "point", "offset": {"years": 1}},
        "ar_index": {"op": "index", "of": "ar_balance", "offset": {"years": 1}},
        "yoy_contribution": {
            "op": "contribution",
            "of": "ar_balance",
            "vs": "ar_last_year",
            "cuts": ["G"],
        },
        # L7
        "worst_offender": {
            "op": "top_n",
            "of": "overdue_amount",
            "n": 2,
            "order": "desc",
            "cuts": ["G"],
        },
        "risk_quartile": {
            "op": "ntile",
            "of": "overdue_amount",
            "tiles": 4,
            "order": "desc",
            "cuts": ["G"],
        },
        "overdue_percent_rank": {
            "op": "percent_rank",
            "of": "overdue_amount",
            "order": "desc",
            "cuts": ["G"],
        },
    }
    for name in BUCKETS:
        measures[f"aging_{name}"] = {"of": f"bucket_{name}", "op": "point", "offset": now}
    measures["aging_sum_check"] = {
        "op": "expr",
        "expr": " + ".join(f"aging_{name}" for name in BUCKETS),
    }
    return measures


def _ar_kpi(kpi_id: int) -> dict:
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "open_invoice",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "region", "from": "region"},
            {"name": "segment", "from": "segment"},
            {"name": "currency", "from": "currency"},
            {"name": "customer_id", "from": "customer_id"},
            {"name": "credit_rating", "from": "credit_rating"},
        ],
        "default_dimensions": ["region"],
        "base_measures": _base_measures(),
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": _measures(),
        "green_when": {"of": "overdue_rate", "below": 40},
    }


def _model() -> dict:
    return {
        "model_id": "open_invoice",
        "kind": "physical",
        "required_aliases": ["open_invoice"],
        "sources": {"open_invoice": {"alias": "open_invoice"}},
        "joins": [],
    }


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _setup(tmp_path, extra_config, kpi_id: int, measures: list[str], **kwargs):
    write_yaml(extra_config / "models" / "finance" / "open_invoice.yaml", _model())
    write_yaml(extra_config / "kpis" / "finance" / f"{kpi_id}.yaml", _ar_kpi(kpi_id))
    ctx = make_context(
        _parquet(tmp_path),
        measures=measures,
        kpi_id=kpi_id,
        month=kwargs.pop("month", "2026-03"),
        **kwargs,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(FACT_COLUMNS)
    ctx["datasets"]["Sotif"]["alias"] = "open_invoice"
    return ctx


def _run(ctx, extra_config) -> dict:
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
# L1 — FX normalisation
# --------------------------------------------------------------------------


def test_ar_l1_fx_normalised_balances(tmp_path, extra_config):
    """A lookup rate feeds a three-step row chain before anything is summed."""
    ctx = _setup(tmp_path, extra_config, 9801, L1_MEASURES, selected_dimensions=["currency"])
    result = _run(ctx, extra_config)

    expected = _by_grain(ANCHOR, ("currency",))
    assert len(result["rows"]) == 4
    for (currency,), want in expected.items():
        row = _pick(result, currency=currency)
        assert row["billed"] == pytest.approx(want["billed_usd"])
        assert row["collected"] == pytest.approx(want["collected_usd"])
        assert row["ar_balance"] == pytest.approx(want["outstanding_usd"])
        assert row["ar_balance"] == pytest.approx(row["billed"] - row["collected"])
        assert row["invoices"] == pytest.approx(want["invoices"])

    # CUST-5 bills 4,000,000 + 2,000,000 + 1,000,000 INR at 0.012.
    inr = _pick(result, currency="INR")
    assert inr["billed"] == pytest.approx(7_000_000 * 0.012)
    assert inr["invoices"] == pytest.approx(3.0)
    # EUR is a single Enterprise customer at 1.08.
    eur = _pick(result, currency="EUR")
    assert eur["billed"] == pytest.approx(140_000 * 1.08)


def test_ar_l1_totals_are_currency_agnostic(tmp_path, extra_config):
    """The company balance is the sum of the converted rows, not of local amounts."""
    ctx = _setup(tmp_path, extra_config, 9802, L1_MEASURES, selected_dimensions=[])
    result = _run(ctx, extra_config)
    truth = _by_grain(ANCHOR, ())[()]

    row = result["rows"][0]
    assert row["billed"] == pytest.approx(truth["billed_usd"])
    assert row["ar_balance"] == pytest.approx(truth["outstanding_usd"])

    local_only = float(
        _frame()[pd.to_datetime(_frame()["event_month"]).dt.date == ANCHOR]["invoice_amount"].sum()
    )
    assert row["billed"] != pytest.approx(local_only)


# --------------------------------------------------------------------------
# L2 — aging buckets
# --------------------------------------------------------------------------


def test_ar_l2_aging_buckets_reconcile(tmp_path, extra_config):
    """Five CASE buckets partition the balance exactly, with no leakage."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9803,
        L2_MEASURES + ["aging_sum_check"],
        selected_dimensions=["region"],
    )
    result = _run(ctx, extra_config)

    for (region,), want in _by_grain(ANCHOR, ("region",)).items():
        row = _pick(result, region=region)
        for name in BUCKETS:
            assert row[f"aging_{name}"] == pytest.approx(want[f"aging_{name}"]), (region, name)
        assert row["aging_sum_check"] == pytest.approx(want["outstanding_usd"])
        assert row["aging_sum_check"] == pytest.approx(row["ar_balance"])
        assert row["overdue_amount"] == pytest.approx(want["overdue_usd"])
        assert row["overdue_rate"] == pytest.approx(
            want["overdue_usd"] * 100.0 / want["outstanding_usd"]
        )
        # Current is exactly the balance that is not overdue.
        assert row["ar_balance"] - row["aging_current"] == pytest.approx(row["overdue_amount"])

    # Every bucket carries balance somewhere in the ledger this month.
    company = _by_grain(ANCHOR, ())[()]
    assert all(company[f"aging_{name}"] > 0 for name in BUCKETS), company

    # NA is CUST-1 (DPD 50 and 100) and CUST-2 (DPD 5 and 95), both in USD.
    na = _pick(result, region="NA")
    assert na["aging_current"] == pytest.approx((100000 + 60000) * (1 - 0.58))
    assert na["aging_b1_30"] == pytest.approx(60000 * 0.5 * 0.70)
    assert na["aging_b31_60"] == pytest.approx(100000 * 0.5 * 0.70)
    assert na["aging_b61_90"] == pytest.approx(0.0)
    assert na["aging_b90_plus"] == pytest.approx((100000 + 60000) * 0.25)
    assert na["ar_balance"] == pytest.approx(163200.0)


# --------------------------------------------------------------------------
# L3 — DSO
# --------------------------------------------------------------------------


def test_ar_l3_days_sales_outstanding(tmp_path, extra_config):
    """DSO = balance / billings × days, per segment and for the company."""
    ctx = _setup(tmp_path, extra_config, 9804, L3_MEASURES, selected_dimensions=["segment"])
    result = _run(ctx, extra_config)

    for (segment,), want in _by_grain(ANCHOR, ("segment",)).items():
        row = _pick(result, segment=segment)
        assert row["dso"] == pytest.approx(
            want["outstanding_usd"] / want["billed_usd"] * DAYS_IN_PERIOD
        )

    total_ctx = _setup(tmp_path, extra_config, 9805, L3_MEASURES, selected_dimensions=[])
    total = _run(total_ctx, extra_config)["rows"][0]
    truth = _by_grain(ANCHOR, ())[()]
    assert total["dso"] == pytest.approx(
        truth["outstanding_usd"] / truth["billed_usd"] * DAYS_IN_PERIOD
    )
    # Equivalent framing: the uncollected share of the book, expressed in days.
    collected_share = truth["collected_usd"] / truth["billed_usd"]
    assert total["dso"] == pytest.approx((1.0 - collected_share) * DAYS_IN_PERIOD)
    assert 0.0 < total["dso"] < DAYS_IN_PERIOD


# --------------------------------------------------------------------------
# L4 — IFRS 9 provision matrix
# --------------------------------------------------------------------------


def test_ar_l4_expected_credit_loss(tmp_path, extra_config):
    """A per-row provision rate from the aging matrix, then a coverage ratio."""
    ctx = _setup(tmp_path, extra_config, 9806, L4_MEASURES, selected_dimensions=["credit_rating"])
    result = _run(ctx, extra_config)

    for (rating,), want in _by_grain(ANCHOR, ("credit_rating",)).items():
        row = _pick(result, credit_rating=rating)
        assert row["expected_credit_loss"] == pytest.approx(want["ecl_usd"])
        assert row["coverage_rate"] == pytest.approx(
            want["ecl_usd"] * 100.0 / want["outstanding_usd"]
        )

    total_ctx = _setup(tmp_path, extra_config, 9807, L4_MEASURES, selected_dimensions=[])
    total = _run(total_ctx, extra_config)["rows"][0]
    truth = _by_grain(ANCHOR, ())[()]

    by_hand = sum(
        truth[f"aging_{name}"] * PROVISION[name] for name in BUCKETS
    )
    assert total["expected_credit_loss"] == pytest.approx(by_hand)
    assert total["expected_credit_loss"] == pytest.approx(truth["ecl_usd"])
    # The provision must sit between the best and worst single-bucket rate.
    assert 0.5 < total["coverage_rate"] < 40.0


# --------------------------------------------------------------------------
# L5 — collection effectiveness index
# --------------------------------------------------------------------------


def test_ar_l5_collection_effectiveness_index(tmp_path, extra_config):
    """CEI needs last month's closing balance as this month's opening book."""
    ctx = _setup(tmp_path, extra_config, 9808, L5_MEASURES, selected_dimensions=["region"])
    result = _run(ctx, extra_config)

    opening = _by_grain(MONTHS[LAST - 1], ("region",))
    for (region,), want in _by_grain(ANCHOR, ("region",)).items():
        row = _pick(result, region=region)
        begin = opening[(region,)]["outstanding_usd"]
        numerator = begin + want["billed_usd"] - want["outstanding_usd"]
        denominator = begin + want["billed_usd"] - want["aging_current"]
        assert row["opening_ar"] == pytest.approx(begin)
        assert row["cei"] == pytest.approx(numerator * 100.0 / denominator)
        # CEI is bounded by 100: you cannot collect more than was collectable.
        assert 0.0 < row["cei"] <= 100.0


# --------------------------------------------------------------------------
# L6 — year on year
# --------------------------------------------------------------------------


def test_ar_l6_yoy_index_and_contribution(tmp_path, extra_config):
    """Index vs March 2025 and each region's share of the movement."""
    ctx = _setup(tmp_path, extra_config, 9809, L6_MEASURES, selected_dimensions=["region"])
    result = _run(ctx, extra_config)

    now = _by_grain(ANCHOR, ("region",))
    then = _by_grain(date(2025, 3, 1), ("region",))
    deltas = {
        region: now[(region,)]["outstanding_usd"] - then[(region,)]["outstanding_usd"]
        for (region,) in now
    }
    movement = sum(deltas.values())

    for (region,), want in now.items():
        row = _pick(result, region=region)
        baseline = then[(region,)]["outstanding_usd"]
        assert row["ar_last_year"] == pytest.approx(baseline)
        assert row["ar_index"] == pytest.approx(want["outstanding_usd"] * 100.0 / baseline)
        assert row["yoy_contribution"] == pytest.approx(deltas[region] * 100.0 / movement)

    assert sum(_pick(result, region=r)["yoy_contribution"] for r in ("NA", "EU", "APAC")) == (
        pytest.approx(100.0)
    )


# --------------------------------------------------------------------------
# L7 — customer risk ranking
# --------------------------------------------------------------------------


def test_ar_l7_customer_risk_ranking(tmp_path, extra_config):
    """Top-2 exposure flag, quartiles and percent rank across six customers."""
    ctx = _setup(tmp_path, extra_config, 9810, L7_MEASURES, selected_dimensions=["customer_id"])
    result = _run(ctx, extra_config)

    overdue = {
        customer: want["overdue_usd"]
        for (customer,), want in _by_grain(ANCHOR, ("customer_id",)).items()
    }
    assert len(result["rows"]) == len(overdue) == 6
    names = list(overdue)
    ranks = dict(zip(names, _sql_ranks([overdue[name] for name in names])))
    n = len(names)

    for customer, value in overdue.items():
        row = _pick(result, customer_id=customer)
        rank = ranks[customer]
        assert row["overdue_amount"] == pytest.approx(value)
        assert row["worst_offender"] == pytest.approx(1.0 if rank <= 2 else 0.0)
        assert row["risk_quartile"] == -(-rank * 4 // n)
        assert row["overdue_percent_rank"] == pytest.approx((rank - 1) * 100.0 / (n - 1))

    leader = max(overdue, key=lambda name: overdue[name])
    assert _pick(result, customer_id=leader)["overdue_percent_rank"] == pytest.approx(0.0)
    assert _pick(result, customer_id=leader)["worst_offender"] == pytest.approx(1.0)
    assert sum(row["worst_offender"] for row in result["rows"]) == pytest.approx(2.0)
