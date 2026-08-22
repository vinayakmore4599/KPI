"""End-to-end compute: host-spelled yyyymm extract through Pandas measures.

What this file provides
    Seeded-random parquet (Amount / Event_Month as yyyymm) plus an independent
    oracle. Every case calls compute() so a stop-after-DuckDB failure is visible.

Where it is used
    pytest tests/test_e2e_extract_to_measures.py.

When to use
    Keep this as the regression for mixed-case retrieve columns and a previous-
    year lookback when the selected period is 202607.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.dates import add_months, month_range_inclusive
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml

SEED = 202607
ANCHOR = date(2026, 7, 1)
PRIOR = date(2025, 7, 1)
HOST_COLUMNS = ["Event_Month", "Region", "Reason_Code", "supplier_name", "Amount"]


def _yyyymm(day: date) -> int:
    """Encode a first-of-month date as yyyymm (202607)."""
    return day.year * 100 + day.month


def _parse_yyyymm(value: int) -> date:
    """Decode a yyyymm integer to the first of that month."""
    text = str(int(value)).zfill(6)
    return date(int(text[:4]), int(text[4:6]), 1)


def _random_facts(
    path: Path,
    rng: random.Random,
    *,
    drop_july_2025_na_late: bool = False,
) -> pd.DataFrame:
    """Two irregular Amount rows per grain per month from Jul 2025 through Jul 2026."""
    rows: list[dict] = []
    for month in month_range_inclusive(PRIOR, ANCHOR):
        for region, reason in (
            ("NA", "LATE_SUPPLIER"),
            ("EU", "LATE_SUPPLIER"),
            ("NA", "OTHER"),
        ):
            if (
                drop_july_2025_na_late
                and month == PRIOR
                and region == "NA"
                and reason == "LATE_SUPPLIER"
            ):
                continue
            for _ in range(2):
                rows.append(
                    {
                        "Event_Month": _yyyymm(month),
                        "Region": region,
                        "Reason_Code": reason,
                        "supplier_name": "ABC",
                        "Amount": round(rng.uniform(1.0, 100.0), 4),
                    }
                )
    frame = pd.DataFrame(rows)
    frame.to_parquet(path, index=False)
    return frame


def _oracle(frame: pd.DataFrame) -> pd.DataFrame:
    """Sum Amount by calendar month / reason / region from the same parquet."""
    work = frame.copy()
    work["month"] = work["Event_Month"].map(_parse_yyyymm)
    grouped = work.groupby(["month", "Reason_Code", "Region"], as_index=False)["Amount"].sum()
    return grouped.rename(columns={"Amount": "amount"})


def _at(
    oracle: pd.DataFrame,
    month: date,
    reason: str,
    region: str | None = None,
) -> float | None:
    """Oracle sum at one month. region=None adds every region (cut G)."""
    mask = (oracle["month"] == month) & (oracle["Reason_Code"] == reason)
    if region is not None:
        mask &= oracle["Region"] == region
    hits = oracle.loc[mask]
    if hits.empty:
        return None
    return float(hits["amount"].sum())


def _window(
    oracle: pd.DataFrame,
    end: date,
    months: int,
    reason: str,
    region: str | None = None,
) -> float | None:
    """Inclusive trailing-N-month oracle sum ending at `end`."""
    start = add_months(end, -(months - 1))
    total = 0.0
    seen = False
    for month in month_range_inclusive(start, end):
        value = _at(oracle, month, reason, region)
        if value is None:
            continue
        seen = True
        total += value
    return total if seen else None


def _growth(current: float | None, previous: float | None) -> float | None:
    """Same null/zero-base rule as catalog growth_pct."""
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous


def _yyyymm_kpi(kpi_id: int, *, model: str = "sotif") -> dict:
    """3004-shaped KPI with yyyymm time and previous-year / window / YoY keys."""
    spec = minimal_kpi(kpi_id, model=model)
    spec["time"]["format"] = "yyyymm"
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "previous_year_value": {
            "of": "sotif_value",
            "op": "point",
            "offset": {"years": 1},
        },
        "value_3m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 3},
            "inclusive": True,
        },
        "value_6m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 6},
            "inclusive": True,
        },
        "value_12m": {
            "of": "sotif_value",
            "op": "window",
            "trailing": {"months": 12},
            "inclusive": True,
        },
        "yoy_month": {
            "op": "arithmetic",
            "fn": "growth_pct",
            "left": "current_value",
            "right": "previous_year_value",
        },
        "trend_12m": {
            "of": "sotif_value",
            "op": "trend",
            "trailing": {"months": 12},
            "inclusive": True,
            "cuts": ["G"],
        },
    }
    return spec


def _host_context(
    parquet_path: Path,
    *,
    measures: list[str],
    kpi_id: int,
    region: list[str] | None = None,
) -> dict:
    """Context listing host column spellings and the 202607 period filter."""
    ctx = make_context(
        parquet_path,
        measures=measures,
        supplier=["ABC"],
        month="202607",
        kpi_id=kpi_id,
        region=region,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(HOST_COLUMNS)
    return ctx


def _approx(actual, expected):
    """Compare a computed measure to the oracle, including nulls."""
    if expected is None:
        assert actual is None or (isinstance(actual, float) and pd.isna(actual))
        return
    assert actual is not None
    assert actual == pytest.approx(expected)


def _assert_requested_only(rows: list[dict], requested: list[str]) -> None:
    """Every row has the requested keys and does not invent unrequested catalog keys."""
    assert rows
    extra = {"trend_12m"}
    for row in rows:
        for key in requested:
            assert key in row, (key, row)
        for key in extra:
            if key not in requested:
                assert key not in row


def test_yyyymm_current_and_previous_year(tmp_path, extra_config):
    """202607 + previous_year_value scans Jul 2025–Jul 2026 and both scalars match."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9901.yaml", _yyyymm_kpi(9901))
    ctx = _host_context(
        facts, measures=["current_value", "previous_year_value"], kpi_id=9901
    )

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 12
    assert planned["span_start"] == "2025-07-01"
    sql = planned["sql"]
    assert ">=" in sql and "<" in sql
    assert '"event_month" IN' not in " ".join(sql.split())

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(result["rows"], ["current_value", "previous_year_value"])
    assert result["parameters"]["lookback_months"] == 12
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER"))
    _approx(g["previous_year_value"], _at(oracle, PRIOR, "LATE_SUPPLIER"))
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    _approx(na["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER", "NA"))
    _approx(na["previous_year_value"], _at(oracle, PRIOR, "LATE_SUPPLIER", "NA"))


def test_yyyymm_yoy_only_walks_previous_year(tmp_path, extra_config):
    """Requesting only yoy_month still widens 12 months and emits only YoY."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9902.yaml", _yyyymm_kpi(9902))
    ctx = _host_context(facts, measures=["yoy_month"], kpi_id=9902)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 12
    assert planned["span_start"] == "2025-07-01"

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(result["rows"], ["yoy_month"])
    for row in result["rows"]:
        assert "current_value" not in row
        assert "previous_year_value" not in row
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(
        g["yoy_month"],
        _growth(_at(oracle, ANCHOR, "LATE_SUPPLIER"), _at(oracle, PRIOR, "LATE_SUPPLIER")),
    )


def test_yyyymm_windows_match_oracle(tmp_path, extra_config):
    """Inclusive 3/6/12m windows at 202607 match the independent trailing sums."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 1))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9903.yaml", _yyyymm_kpi(9903))
    ctx = _host_context(
        facts, measures=["value_3m", "value_6m", "value_12m"], kpi_id=9903
    )

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 11
    assert planned["span_start"] == "2025-08-01"

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(result["rows"], ["value_3m", "value_6m", "value_12m"])
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["value_3m"], _window(oracle, ANCHOR, 3, "LATE_SUPPLIER"))
    _approx(g["value_6m"], _window(oracle, ANCHOR, 6, "LATE_SUPPLIER"))
    _approx(g["value_12m"], _window(oracle, ANCHOR, 12, "LATE_SUPPLIER"))
    other = find_row(result, cut="G", reason="OTHER")
    _approx(other["value_3m"], _window(oracle, ANCHOR, 3, "OTHER"))


def test_yyyymm_current_only_stays_one_month(tmp_path, extra_config):
    """current_value alone may keep a July-only span and still compute."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 2))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9904.yaml", _yyyymm_kpi(9904))
    ctx = _host_context(facts, measures=["current_value"], kpi_id=9904)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 0
    assert planned["span_start"] == "2026-07-01"

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(result["rows"], ["current_value"])
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER"))


def test_folded_previous_year_measure_key(tmp_path, extra_config):
    """Previous_Year_Value folds onto previous_year_value and widens the span."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 3))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9905.yaml", _yyyymm_kpi(9905))
    ctx = _host_context(facts, measures=["Previous_Year_Value"], kpi_id=9905)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 12
    assert planned["span_start"] == "2025-07-01"

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(result["rows"], ["previous_year_value"])
    g = find_row(result, cut="G", reason="OTHER")
    _approx(g["previous_year_value"], _at(oracle, PRIOR, "OTHER"))


def test_missing_prior_month_is_null_current_still_computes(tmp_path, extra_config):
    """Dropping Jul 2025 NA LATE_SUPPLIER nulls that prior year; current stays numeric."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 4), drop_july_2025_na_late=True)
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9906.yaml", _yyyymm_kpi(9906))
    ctx = _host_context(
        facts, measures=["current_value", "previous_year_value"], kpi_id=9906
    )

    result = compute(ctx, config_dir=extra_config)
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    _approx(na["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER", "NA"))
    _approx(na["previous_year_value"], None)
    eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    _approx(eu["previous_year_value"], _at(oracle, PRIOR, "LATE_SUPPLIER", "EU"))
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["previous_year_value"], _at(oracle, PRIOR, "LATE_SUPPLIER"))


def test_g_adds_regions_r_keeps_region_filter(tmp_path, extra_config):
    """G ignores Region=NA and adds EU; R NA is NA only."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 5))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9907.yaml", _yyyymm_kpi(9907))
    ctx = _host_context(
        facts, measures=["current_value"], kpi_id=9907, region=["NA"]
    )

    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER"))
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    _approx(na["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER", "NA"))
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}
    assert g["current_value"] != pytest.approx(na["current_value"])


def test_sql_model_agrees_with_physical_oracle(tmp_path, extra_config):
    """kind: sql wrapping the same TitleCase parquet matches the physical oracle."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 6))
    oracle = _oracle(frame)
    write_yaml(
        extra_config / "models" / "sotif_host.yaml",
        {
            "model_id": "sotif_host",
            "kind": "sql",
            "required_aliases": ["sotif"],
            "output_schema": [
                {"name": "Event_Month", "type": "integer"},
                {"name": "Region", "type": "varchar"},
                {"name": "Reason_Code", "type": "varchar"},
                {"name": "supplier_name", "type": "varchar"},
                {"name": "Amount", "type": "decimal"},
            ],
            "sql": (
                'SELECT "Event_Month", "Region", "Reason_Code", supplier_name, "Amount"\n'
                "FROM read_parquet($sotif_path)\n"
            ),
        },
    )
    write_yaml(extra_config / "kpis" / "9908.yaml", _yyyymm_kpi(9908, model="sotif_host"))
    ctx = _host_context(
        facts, measures=["current_value", "previous_year_value", "yoy_month"], kpi_id=9908
    )

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 12
    assert "read_parquet(?)" in planned["sql"]

    result = compute(ctx, config_dir=extra_config)
    _assert_requested_only(
        result["rows"], ["current_value", "previous_year_value", "yoy_month"]
    )
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    current = _at(oracle, ANCHOR, "LATE_SUPPLIER")
    prior = _at(oracle, PRIOR, "LATE_SUPPLIER")
    _approx(g["current_value"], current)
    _approx(g["previous_year_value"], prior)
    _approx(g["yoy_month"], _growth(current, prior))


def test_empty_measures_required_does_not_invent_catalog(tmp_path, extra_config):
    """Empty measures_required does not crash or expand to every YAML key."""
    facts = tmp_path / "facts.parquet"
    _random_facts(facts, random.Random(SEED + 7))
    write_yaml(extra_config / "kpis" / "9909.yaml", _yyyymm_kpi(9909))
    ctx = _host_context(facts, measures=[], kpi_id=9909)

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 0
    assert planned["span_start"] == "2026-07-01"

    result = compute(ctx, config_dir=extra_config)
    catalog = {
        "current_value",
        "previous_year_value",
        "value_3m",
        "value_6m",
        "value_12m",
        "yoy_month",
        "trend_12m",
    }
    for row in result["rows"]:
        assert catalog.isdisjoint(row)
    assert result["parameters"]["lookback_months"] == 0


def test_host_measures_requested_computes_previous_year(tmp_path, extra_config):
    """Host `measures requested` + folded keys still widen 202607 and emit numbers."""
    facts = tmp_path / "facts.parquet"
    frame = _random_facts(facts, random.Random(SEED + 8))
    oracle = _oracle(frame)
    write_yaml(extra_config / "kpis" / "9910.yaml", _yyyymm_kpi(9910))
    ctx = _host_context(facts, measures=[], kpi_id=9910)
    view = ctx["execution"]["view_details"][0]
    view.pop("measures_required", None)
    view["measures requested"] = [
        {"measure_key": "current_value"},
        {"measure_key": "previousyearvalue"},
        {"MeasureKey": "value_3m"},
        {"measure_key": "value_6m"},
    ]

    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 12
    assert planned["span_start"] == "2025-07-01"

    result = compute(ctx, config_dir=extra_config)
    assert result["parameters"]["lookback_months"] == 12
    _assert_requested_only(
        result["rows"], ["current_value", "previous_year_value", "value_3m", "value_6m"]
    )
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    _approx(g["current_value"], _at(oracle, ANCHOR, "LATE_SUPPLIER"))
    _approx(g["previous_year_value"], _at(oracle, PRIOR, "LATE_SUPPLIER"))
    _approx(g["value_3m"], _window(oracle, ANCHOR, 3, "LATE_SUPPLIER"))
    _approx(g["value_6m"], _window(oracle, ANCHOR, 6, "LATE_SUPPLIER"))
    assert g["current_value"] is not None
    assert g["previous_year_value"] is not None
