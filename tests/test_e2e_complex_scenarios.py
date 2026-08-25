"""End-to-end compute: complex measure graphs across request grains.

What this file provides
    Full adapt → bind → DuckDB extract → Pandas calc → JSON for nested
    arithmetic, windows, YoY, rank/share, trends, add-on ops, non-additive
    aggs, two-model joins, and snapshot KPIs. Request grain is overlaid with
    selected_dimensions. Numbers are checked against an independent pandas
    oracle, not against another engine path.

Where it is used
    pytest tests/test_e2e_complex_scenarios.py.

When to use
    Add a case when a new op, grain overlay, or G/R interaction must hold
    together in one request. Keep unit planner tests elsewhere.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.dates import add_months, month_range_inclusive
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml

ANCHOR = date(2026, 3, 1)
PRIOR = date(2025, 3, 1)

KITCHEN_KEYS = [
    "reason_code",
    "current_value",
    "previous_year_value",
    "value_3m",
    "value_qtd",
    "value_3m_ly",
    "yoy_month",
    "blended",
    "share_of_3m",
    "target",
    "gap",
    "hit",
    "vs_goal",
    "volume_index",
    "reason_rank",
    "percent_gt",
    "trend_12m",
]

G_ONLY = {"reason_rank", "percent_gt", "trend_12m"}


def _as_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize event_month to date for oracle masks."""
    out = frame.copy()
    out["event_month"] = pd.to_datetime(out["event_month"]).dt.date
    return out


def _sum_at(frame: pd.DataFrame, month: date, **dims: str | None) -> float:
    """Oracle sum of amount at one month and optional dimension equals."""
    mask = frame["event_month"] == month
    for column, value in dims.items():
        if value is not None:
            mask &= frame[column] == value
    return float(pd.to_numeric(frame.loc[mask, "amount"], errors="coerce").sum())


def _window_sum(frame: pd.DataFrame, end: date, months: int, **dims: str | None) -> float:
    """Inclusive trailing-N-month oracle."""
    start = add_months(end, -(months - 1))
    return sum(_sum_at(frame, month, **dims) for month in month_range_inclusive(start, end))


def _growth(current: float | None, previous: float | None) -> float | None:
    """Same null/zero-base rule as growth_pct."""
    if current is None or previous in (None, 0):
        return None
    return float((current - previous) / previous)


def _approx(actual, expected) -> None:
    """Compare a computed measure to the oracle, including nulls."""
    if expected is None:
        assert actual is None or (isinstance(actual, float) and pd.isna(actual))
        return
    assert actual == pytest.approx(expected)


def _pick(result: dict, *, cut: str, **dims: str | None) -> dict:
    """Unique result row for a cut plus optional catalog dims."""
    matches = []
    for row in result["rows"]:
        if row.get("output_cut") != cut:
            continue
        if all(row.get(name) == value for name, value in dims.items()):
            matches.append(row)
    assert matches, (cut, dims, result["rows"])
    assert len(matches) == 1, (cut, dims, matches)
    return matches[0]


def _g_only_ok(row: dict, keys: list[str]) -> None:
    """Every requested key is present except cut-limited ops on R."""
    for key in keys:
        if key in G_ONLY and row["output_cut"] != "G":
            assert key not in row
            continue
        assert key in row, (key, row)


def _kitchen_kpi(kpi_id: int) -> dict:
    """One KPI: points, windows, YoY, expr, fn, rank, share, trend, add-on ops."""
    spec = minimal_kpi(kpi_id)
    spec["green_when"] = {"above": 40, "of": "current_value"}
    spec["measures"]["reason_code"] = {"kind": "dimension"}
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"years": 1},
    }
    spec["measures"]["value_qtd"] = {"of": "sotif_value", "op": "window", "range": "qtd"}
    spec["measures"]["value_3m_ly"] = {
        "op": "lag",
        "of": "value_3m",
        "offset": {"years": 1},
    }
    spec["measures"]["yoy_month"] = {
        "op": "arithmetic",
        "fn": "growth_pct",
        "left": "current_value",
        "right": "previous_year_value",
    }
    spec["measures"]["blended"] = {
        "op": "expr",
        "expr": "(current_value + previous_year_value) / 2",
    }
    spec["measures"]["share_of_3m"] = {
        "op": "fn",
        "fn": "percent",
        "inputs": ["current_value", "value_3m"],
    }
    spec["measures"]["target"] = {"op": "constant", "value": 40}
    spec["measures"]["gap"] = {
        "op": "vs_target",
        "of": "current_value",
        "vs": "target",
        "as": "gap",
    }
    spec["measures"]["hit"] = {
        "op": "threshold",
        "of": "current_value",
        "cmp": "gte",
        "value": 40,
    }
    spec["measures"]["vs_goal"] = {
        "op": "fn",
        "fn": "attainment",
        "inputs": ["current_value", "target"],
    }
    spec["measures"]["volume_index"] = {
        "op": "index",
        "of": "current_value",
        "offset": {"years": 1},
    }
    spec["measures"]["reason_rank"] = {
        "op": "rank",
        "of": "current_value",
        "order": "desc",
        "cuts": ["G"],
    }
    spec["measures"]["percent_gt"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "cuts": ["G"],
    }
    spec["measures"]["trend_12m"] = {
        "of": "sotif_value",
        "op": "trend",
        "trailing": {"months": 12},
        "inclusive": True,
        "cuts": ["G"],
    }
    return spec


def _write_kitchen(extra_config: Path, kpi_id: int) -> None:
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", _kitchen_kpi(kpi_id))


def _two_supplier_facts(path: Path) -> pd.DataFrame:
    """ABC matches the shared fixture; XYZ adds a second rank/share universe."""
    rows: list[dict] = []
    combos = (
        ("ABC", "NA", "LATE_SUPPLIER", 10),
        ("ABC", "EU", "LATE_SUPPLIER", 5),
        ("ABC", "NA", "OTHER", 2),
        ("XYZ", "NA", "LATE_SUPPLIER", 8),
        ("XYZ", "EU", "OTHER", 4),
    )
    for month in month_range_inclusive(date(2025, 1, 1), ANCHOR):
        for supplier, region, reason, base in combos:
            if (
                supplier == "ABC"
                and region == "NA"
                and reason == "LATE_SUPPLIER"
                and month == PRIOR
            ):
                continue
            rows.append(
                {
                    "event_month": month,
                    "region": region,
                    "reason_code": reason,
                    "supplier_name": supplier,
                    "amount": base * month.month,
                }
            )
    frame = pd.DataFrame(rows)
    frame.to_parquet(path, index=False)
    return frame


def _sum_cut(result: dict, cut: str, key: str, **dims: str | None) -> float:
    """Sum a numeric measure across matching rows (nulls as 0)."""
    total = 0.0
    for row in result["rows"]:
        if row.get("output_cut") != cut:
            continue
        if any(row.get(name) != value for name, value in dims.items()):
            continue
        value = row.get(key)
        if value is not None:
            total += float(value)
    return total


def test_e2e_kitchen_sink_default_grain(parquet_path, extra_config):
    """One request: nested calc graph at default reason grain, numbers vs oracle."""
    _write_kitchen(extra_config, 9930)
    ctx = make_context(
        parquet_path,
        measures=KITCHEN_KEYS,
        supplier=["ABC"],
        kpi_id=9930,
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["lookback_months"] >= 12
    assert planned["selected_dimensions"] == ["reason_code"]
    assert "rows" not in planned

    result = compute(ctx, config_dir=extra_config)
    assert result["sql"] == planned["sql"]
    assert result["selected_dimensions"] == ["reason_code"]
    for row in result["rows"]:
        _g_only_ok(row, KITCHEN_KEYS)

    frame = _as_dates(pd.read_parquet(parquet_path))
    g_late_now = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER")
    g_late_py = _sum_at(frame, PRIOR, reason_code="LATE_SUPPLIER")
    g_late_3m = _window_sum(frame, ANCHOR, 3, reason_code="LATE_SUPPLIER")
    g_late_3m_ly = _window_sum(frame, PRIOR, 3, reason_code="LATE_SUPPLIER")
    g_other_now = _sum_at(frame, ANCHOR, reason_code="OTHER")
    na_late_now = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER", region="NA")
    na_late_py = _sum_at(frame, PRIOR, reason_code="LATE_SUPPLIER", region="NA")

    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(result, cut="G", reason="OTHER")
    na_late = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")

    assert g_late["grouped_dimensions"] == ["reason_code"]
    assert na_late["grouped_dimensions"] == ["reason_code", "region"]
    assert g_late["reason_code"] == "LATE_SUPPLIER"
    _approx(g_late["current_value"], g_late_now)
    _approx(g_late["previous_year_value"], g_late_py)
    _approx(g_late["value_3m"], g_late_3m)
    _approx(g_late["value_qtd"], g_late_3m)
    _approx(g_late["value_3m_ly"], g_late_3m_ly)
    _approx(g_late["yoy_month"], _growth(g_late_now, g_late_py))
    _approx(g_late["blended"], (g_late_now + g_late_py) / 2)
    _approx(g_late["share_of_3m"], g_late_now / g_late_3m * 100)
    assert g_late["target"] == 40
    _approx(g_late["gap"], g_late_now - 40)
    assert g_late["hit"] == 1.0
    _approx(g_late["vs_goal"], g_late_now / 40 * 100)
    _approx(g_late["volume_index"], g_late_now * 100 / g_late_py)
    assert g_late["reason_rank"] == 1
    assert g_other["reason_rank"] == 2
    _approx(g_late["percent_gt"], g_late_now / (g_late_now + g_other_now) * 100)
    _approx(g_other["percent_gt"], g_other_now / (g_late_now + g_other_now) * 100)
    assert len(g_late["trend_12m"]) == 12
    assert g_late["trend_12m"][-1] == pytest.approx(g_late_now)
    assert result["trend_axes"]["trend_12m"][-1] == "2026-03-01"
    assert g_late["green"] is True
    assert g_other["green"] is False
    _approx(na_late["current_value"], na_late_now)
    assert na_late_py == 0.0
    _approx(na_late["previous_year_value"], None)
    _approx(na_late["yoy_month"], None)
    _approx(na_late["blended"], None)
    assert g_late["current_value"] == pytest.approx(
        _sum_cut(result, "R", "current_value", reason_code="LATE_SUPPLIER")
    )
    assert g_late["current_value"] == 45.0
    assert na_late["current_value"] == 30.0


def test_e2e_kitchen_sink_supplier_and_empty_grain(parquet_path, extra_config):
    """Same graph at [supplier] and []; additive G equals sum of R; dims stamp."""
    _write_kitchen(extra_config, 9931)
    base = make_context(
        parquet_path,
        measures=KITCHEN_KEYS,
        supplier=["ABC"],
        kpi_id=9931,
    )
    supplier = compute({**base, "selected_dimensions": ["supplier"]}, config_dir=extra_config)
    empty = compute({**base, "selected_dimensions": []}, config_dir=extra_config)
    default = compute(base, config_dir=extra_config)

    assert supplier["selected_dimensions"] == ["supplier"]
    assert empty["selected_dimensions"] == []
    g_sup = _pick(supplier, cut="G", supplier="ABC")
    assert g_sup["grouped_dimensions"] == ["supplier"]
    assert g_sup.get("reason_code") is None
    assert g_sup["supplier"] == "ABC"
    _approx(g_sup["current_value"], 51.0)
    _approx(g_sup["value_3m"], 102.0)
    _approx(g_sup["previous_year_value"], 21.0)
    _approx(g_sup["yoy_month"], _growth(51.0, 21.0))
    _approx(g_sup["volume_index"], 51.0 * 100 / 21.0)
    assert g_sup["reason_rank"] == 1
    _approx(g_sup["percent_gt"], 100.0)
    assert g_sup["green"] is True
    assert g_sup["current_value"] == pytest.approx(
        _sum_cut(supplier, "R", "current_value", supplier="ABC")
    )
    r_na = _pick(supplier, cut="R", supplier="ABC", region="NA")
    assert r_na["grouped_dimensions"] == ["supplier", "region"]
    _approx(r_na["current_value"], 36.0)
    assert "trend_12m" not in r_na

    g_empty = _pick(empty, cut="G")
    assert g_empty["grouped_dimensions"] == []
    assert g_empty.get("reason_code") is None
    assert g_empty.get("region") is None
    _approx(g_empty["current_value"], 51.0)
    _approx(g_empty["value_3m"], 102.0)
    _approx(g_empty["percent_gt"], 100.0)
    assert g_empty["current_value"] == pytest.approx(_sum_cut(empty, "R", "current_value"))

    g_late = find_row(default, cut="G", reason="LATE_SUPPLIER")
    assert g_late["current_value"] != pytest.approx(g_sup["current_value"])
    assert set(g_late) >= set(KITCHEN_KEYS)
    assert set(g_sup) >= set(KITCHEN_KEYS) - {"reason_code"}


def test_e2e_rank_and_share_universe_follows_selected_dimensions(tmp_path, extra_config):
    """Rank/share at reason grain are LATE vs OTHER; at supplier grain ABC vs XYZ."""
    path = tmp_path / "two_sup.parquet"
    frame = _as_dates(_two_supplier_facts(path))
    _write_kitchen(extra_config, 9932)
    ctx = make_context(path, measures=KITCHEN_KEYS, kpi_id=9932)
    by_reason = compute(ctx, config_dir=extra_config)
    by_supplier = compute(
        {**ctx, "selected_dimensions": ["supplier"]}, config_dir=extra_config
    )

    abc_now = _sum_at(frame, ANCHOR, supplier_name="ABC")
    xyz_now = _sum_at(frame, ANCHOR, supplier_name="XYZ")
    late_now = _sum_at(frame, ANCHOR, reason_code="LATE_SUPPLIER")
    other_now = _sum_at(frame, ANCHOR, reason_code="OTHER")

    g_late = find_row(by_reason, cut="G", reason="LATE_SUPPLIER")
    g_other = find_row(by_reason, cut="G", reason="OTHER")
    _approx(g_late["current_value"], late_now)
    _approx(g_other["current_value"], other_now)
    assert g_late["reason_rank"] == 1
    assert g_other["reason_rank"] == 2
    _approx(g_late["percent_gt"], late_now / (late_now + other_now) * 100)

    abc = _pick(by_supplier, cut="G", supplier="ABC")
    xyz = _pick(by_supplier, cut="G", supplier="XYZ")
    _approx(abc["current_value"], abc_now)
    _approx(xyz["current_value"], xyz_now)
    assert abc["reason_rank"] == 1
    assert xyz["reason_rank"] == 2
    _approx(abc["percent_gt"], abc_now / (abc_now + xyz_now) * 100)
    _approx(xyz["percent_gt"], xyz_now / (abc_now + xyz_now) * 100)
    assert abc["current_value"] != pytest.approx(g_late["current_value"])
    assert abc["percent_gt"] != pytest.approx(g_late["percent_gt"])
    assert abc["current_value"] == pytest.approx(
        _sum_cut(by_supplier, "R", "current_value", supplier="ABC")
    )
    assert abc_now == 51.0
    assert xyz_now == 36.0
    assert late_now == 69.0


def test_e2e_region_filter_stays_worldwide_on_g_at_supplier_grain(
    parquet_path, extra_config
):
    """G ignores region at [supplier]; R is NA-only; extract still slices ABC."""
    _write_kitchen(extra_config, 9933)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m", "yoy_month"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9933,
        selected_dimensions=["supplier"],
    )
    result = compute(ctx, config_dir=extra_config)
    g = _pick(result, cut="G", supplier="ABC")
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row.get("supplier") == "ABC"
    }
    assert r_regions == {"NA"}
    _approx(g["current_value"], 51.0)
    _approx(g["value_3m"], 102.0)
    r_na = _pick(result, cut="R", supplier="ABC", region="NA")
    _approx(r_na["current_value"], 36.0)
    _approx(r_na["value_3m"], 72.0)


def test_e2e_median_at_supplier_grain_is_not_median_of_regions(tmp_path, extra_config):
    """Non-additive median uses fact rows at the request grain, not rolled R medians."""
    path = tmp_path / "med_sup.parquet"
    pd.DataFrame(
        [
            _med_row("ABC", "NA", 10),
            _med_row("ABC", "NA", 20),
            _med_row("ABC", "NA", 30),
            _med_row("ABC", "EU", 100),
            _med_row("XYZ", "NA", 1),
            _med_row("XYZ", "NA", 2),
        ]
    ).to_parquet(path, index=False)
    spec = minimal_kpi(
        9934,
        default_dimensions=["reason_code"],
        base_measures={"amount_median": {"sql": "amount", "agg": "median"}},
        measures={
            "median_now": {
                "of": "amount_median",
                "op": "point",
                "offset": {"months": 0},
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9934.yaml", spec)
    ctx = make_context(
        path,
        measures=["median_now"],
        kpi_id=9934,
        selected_dimensions=["supplier"],
    )
    result = compute(ctx, config_dir=extra_config)
    abc = _pick(result, cut="G", supplier="ABC")
    xyz = _pick(result, cut="G", supplier="XYZ")
    r_abc = [
        row["median_now"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row.get("supplier") == "ABC"
    ]
    assert abc["median_now"] == 25.0
    assert xyz["median_now"] == 1.5
    assert sorted(r_abc) == [20.0, 100.0]
    assert abc["median_now"] != pytest.approx(sum(r_abc) / len(r_abc))


def _med_row(supplier: str, region: str, amount: float) -> dict:
    return {
        "event_month": "2026-03-01",
        "region": region,
        "reason_code": "LATE_SUPPLIER",
        "supplier_name": supplier,
        "amount": amount,
    }


def test_e2e_result_not_in_grain_with_extract_supplier(parquet_path, extra_config):
    """Result region filter skips G at supplier grain; extract still slices ABC."""
    spec = minimal_kpi(
        9935,
        cuts=[
            {"name": "G", "group_by": [], "ignore_filters": [], "also_emit": ["R"]},
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
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
            "yoy_month": {
                "op": "arithmetic",
                "fn": "growth_pct",
                "left": "current_value",
                "right": "previous_year_value",
            },
        },
    )
    spec["filters"] = {"region": {"column": "region", "op": "in", "apply": "result"}}
    write_yaml(extra_config / "kpis" / "9935.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m", "yoy_month"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9935,
        selected_dimensions=["supplier"],
    )
    result = compute(ctx, config_dir=extra_config)
    assert {"filter_code": "region", "reason": "not_in_grain"} in result["skipped_filters"]
    g = _pick(result, cut="G", supplier="ABC")
    _approx(g["current_value"], 51.0)
    r_regions = {row["region"] for row in result["rows"] if row["output_cut"] == "R"}
    assert r_regions == {"NA"}
    _approx(_pick(result, cut="R", supplier="ABC", region="NA")["current_value"], 36.0)


def test_e2e_two_model_windows_and_ratio_at_supplier(
    parquet_path, extra_config, tmp_path
):
    """Joined extracts at supplier grain: 3m windows and a spanning ratio."""
    spend = tmp_path / "spend.parquet"
    pd.DataFrame(
        [
            {"event_month": "2026-01-01", "supplier_name": "ABC", "spend": 60},
            {"event_month": "2026-02-01", "supplier_name": "ABC", "spend": 80},
            {"event_month": "2026-03-01", "supplier_name": "ABC", "spend": 100},
        ]
    ).to_parquet(spend, index=False)
    write_yaml(
        extra_config / "models" / "marketing.yaml",
        {
            "model_id": "marketing",
            "kind": "physical",
            "required_aliases": ["marketing"],
            "sources": {"marketing": {"alias": "marketing"}},
            "joins": [],
        },
    )
    write_yaml(
        extra_config / "kpis" / "9936.yaml",
        {
            "kpi_id": 9936,
            "version": 1,
            "model": "sotif",
            "time": {
                "column": "event_month",
                "grain": "month",
                "filter_code": "reporting_month",
            },
            "dimensions": [
                {"name": "reason_code", "from": "reason_code"},
                {"name": "supplier", "from": "supplier_name"},
            ],
            "default_dimensions": ["reason_code"],
            "base_measures": {
                "sotif_value": {"sql": "amount", "agg": "sum", "model": "sotif"},
                "marketing_spend": {"sql": "spend", "agg": "sum", "model": "marketing"},
            },
            "model_relations": [
                {
                    "left": "sotif_value",
                    "right": "marketing_spend",
                    "on": ["event_month", "supplier"],
                    "how": "inner",
                }
            ],
            "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
            "default_cut": "G",
            "measures": {
                "current_sotif": {
                    "of": "sotif_value",
                    "op": "point",
                    "offset": {"months": 0},
                },
                "current_spend": {
                    "of": "marketing_spend",
                    "op": "point",
                    "offset": {"months": 0},
                },
                "sotif_3m": {
                    "of": "sotif_value",
                    "op": "window",
                    "trailing": {"months": 3},
                    "inclusive": True,
                },
                "spend_3m": {
                    "of": "marketing_spend",
                    "op": "window",
                    "trailing": {"months": 3},
                    "inclusive": True,
                },
                "spend_ratio": {
                    "op": "arithmetic",
                    "fn": "div",
                    "left": "current_sotif",
                    "right": "current_spend",
                },
                "window_ratio": {
                    "op": "expr",
                    "expr": "sotif_3m / spend_3m",
                },
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=[
            "current_sotif",
            "current_spend",
            "sotif_3m",
            "spend_3m",
            "spend_ratio",
            "window_ratio",
        ],
        supplier=["ABC"],
        kpi_id=9936,
        selected_dimensions=["supplier"],
        extra_datasets={
            "Marketing": {
                "dataset_id": 40,
                "dataset_name": "MARKETING",
                "table_type": "PARQUET",
                "path": str(spend),
                "alias": "marketing",
                "columns": ["event_month", "supplier_name", "spend"],
                "filter_column_mappings": [],
            }
        },
    )
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True
    assert planned["sqls"] == result["sqls"]
    row = _pick(result, cut="G", supplier="ABC")
    assert row["grouped_dimensions"] == ["supplier"]
    assert row.get("reason_code") is None
    _approx(row["current_sotif"], 51.0)
    _approx(row["current_spend"], 100.0)
    _approx(row["sotif_3m"], 102.0)
    _approx(row["spend_3m"], 240.0)
    _approx(row["spend_ratio"], 0.51)
    _approx(row["window_ratio"], 102.0 / 240.0)


def test_e2e_snapshot_selected_dimensions(parquet_path, extra_config):
    """No time block: overlay still changes GROUP BY; values are all-history sums."""
    spec = minimal_kpi(
        9937,
        time=None,
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "reason_code": {"kind": "dimension"},
        },
    )
    write_yaml(extra_config / "kpis" / "9937.yaml", spec)
    frame = _as_dates(pd.read_parquet(parquet_path))
    abc = frame[frame["supplier_name"] == "ABC"]
    ctx = make_context(
        parquet_path, measures=["current_value", "reason_code"], supplier=["ABC"], kpi_id=9937
    )
    del ctx["filters"]["reporting_month"]

    default = compute(ctx, config_dir=extra_config)
    supplier = compute({**ctx, "selected_dimensions": ["supplier"]}, config_dir=extra_config)
    empty = compute({**ctx, "selected_dimensions": []}, config_dir=extra_config)

    late = find_row(default, cut="G", reason="LATE_SUPPLIER")
    _approx(late["current_value"], float(abc.loc[abc["reason_code"] == "LATE_SUPPLIER", "amount"].sum()))
    g_sup = _pick(supplier, cut="G", supplier="ABC")
    assert g_sup.get("reason_code") is None
    _approx(g_sup["current_value"], float(abc["amount"].sum()))
    g_all = _pick(empty, cut="G")
    _approx(g_all["current_value"], float(abc["amount"].sum()))
    assert default["parameters"]["anchor"] is None
    assert "date_trunc" not in default["sql"].lower()


def test_e2e_output_cut_lock_at_supplier_grain(parquet_path, extra_config):
    """output_cut=R at [supplier] emits only supplier+region rows."""
    spec = _kitchen_kpi(9938)
    spec["parameters"] = {
        "output_cut": {"type": "string", "default": "G", "allowed": ["G", "R"]}
    }
    write_yaml(extra_config / "kpis" / "9938.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9938,
        selected_dimensions=["supplier"],
        parameters={"output_cut": "R"},
    )
    result = compute(ctx, config_dir=extra_config)
    assert {row["output_cut"] for row in result["rows"]} == {"R"}
    for row in result["rows"]:
        assert row["grouped_dimensions"] == ["supplier", "region"]
        assert row["supplier"] == "ABC"
    regions = {row["region"] for row in result["rows"]}
    assert regions == {"NA", "EU"}
    _approx(_sum_cut(result, "R", "current_value"), 51.0)


def test_e2e_validate_sql_matches_compute_at_several_overlays(parquet_path, extra_config):
    """validate and compute compile the same extract at omit / [] / [supplier]."""
    _write_kitchen(extra_config, 9939)
    base = make_context(
        parquet_path, measures=KITCHEN_KEYS, supplier=["ABC"], kpi_id=9939
    )
    overlays = [
        {},
        {"selected_dimensions": []},
        {"selected_dimensions": ["supplier"]},
        {"selected_dimensions": {"names": ["supplier", "reason_code"]}},
        {"selected_dimensions": {"supplier": True, "region": False}},
    ]
    for extra in overlays:
        ctx = {**base, **extra} if extra else base
        planned = validate(ctx, config_dir=extra_config)
        computed = compute(ctx, config_dir=extra_config)
        assert planned["sql"] == computed["sql"]
        assert planned["selected_dimensions"] == computed["selected_dimensions"]
        assert planned["lookback_months"] == computed["parameters"]["lookback_months"]


def test_e2e_partition_by_must_sit_on_request_grain(parquet_path, extra_config):
    """partition_by reason_code on R works at default grain and binds off at [supplier]."""
    spec = minimal_kpi(9940)
    spec["measures"]["share"] = {
        "op": "percent_of_total",
        "of": "current_value",
        "partition_by": ["reason_code"],
        "cuts": ["R"],
    }
    write_yaml(extra_config / "kpis" / "9940.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value", "share"],
        supplier=["ABC"],
        kpi_id=9940,
    )
    default = compute(ctx, config_dir=extra_config)
    na_late = find_row(default, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu_late = find_row(default, cut="R", reason="LATE_SUPPLIER", region="EU")
    _approx(na_late["share"], 30.0 / 45.0 * 100)
    _approx(eu_late["share"], 15.0 / 45.0 * 100)

    with pytest.raises(BindError, match="partition_by 'reason_code' is not in cut 'R'"):
        compute({**ctx, "selected_dimensions": ["supplier"]}, config_dir=extra_config)
    with pytest.raises(BindError, match="partition_by 'reason_code' is not in cut 'R'"):
        compute({**ctx, "selected_dimensions": []}, config_dir=extra_config)


def test_e2e_quarter_time_grain_with_selected_supplier(parquet_path, extra_config):
    """parameters.time_grain=quarter plus selected_dimensions=[supplier] in one request."""
    spec = minimal_kpi(
        9941,
        time={
            "column": "event_month",
            "grain": "month",
            "grains": ["month", "quarter"],
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        parameters={"time_grain": {"type": "string"}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9941.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9941,
        time_grain="quarter",
        selected_dimensions=["supplier"],
    )
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["sql"] == result["sql"]
    assert result["parameters"]["anchor"] == "2026-01-01"
    assert result["parameters"]["time_grain"] == "quarter"
    g = _pick(result, cut="G", supplier="ABC")
    assert g["grouped_dimensions"] == ["supplier"]
    _approx(g["current_value"], 102.0)
    assert g["current_value"] == pytest.approx(
        _sum_cut(result, "R", "current_value", supplier="ABC")
    )


def test_e2e_nested_expr_diamond_and_divide_by_zero(parquet_path, extra_config):
    """Diamond arithmetic plus /0 at a missing prior-year R cell, at two grains."""
    spec = _kitchen_kpi(9942)
    spec["measures"]["yoy_times_share"] = {
        "op": "arithmetic",
        "fn": "mul",
        "left": "yoy_month",
        "right": "share_of_3m",
    }
    spec["measures"]["ratio_cur_py"] = {
        "op": "arithmetic",
        "fn": "div",
        "left": "current_value",
        "right": "previous_year_value",
    }
    write_yaml(extra_config / "kpis" / "9942.yaml", spec)
    keys = ["current_value", "previous_year_value", "yoy_month", "share_of_3m", "yoy_times_share", "ratio_cur_py"]
    ctx = make_context(parquet_path, measures=keys, supplier=["ABC"], kpi_id=9942)

    default = compute(ctx, config_dir=extra_config)
    g_late = find_row(default, cut="G", reason="LATE_SUPPLIER")
    na_late = find_row(default, cut="R", reason="LATE_SUPPLIER", region="NA")
    _approx(g_late["yoy_times_share"], 2.0 * (45.0 / 90.0 * 100))
    _approx(g_late["ratio_cur_py"], 45.0 / 15.0)
    _approx(na_late["previous_year_value"], None)
    _approx(na_late["ratio_cur_py"], None)
    _approx(na_late["yoy_times_share"], None)

    supplier = compute({**ctx, "selected_dimensions": ["supplier"]}, config_dir=extra_config)
    g_abc = _pick(supplier, cut="G", supplier="ABC")
    _approx(g_abc["ratio_cur_py"], 51.0 / 21.0)
    _approx(g_abc["yoy_month"], _growth(51.0, 21.0))
    _approx(g_abc["yoy_times_share"], _growth(51.0, 21.0) * (51.0 / 102.0 * 100))
