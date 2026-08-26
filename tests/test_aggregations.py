"""Aggregation tests: avg as sum/count, min, max, count over points and windows.

What this file provides
    Window avg is weighted (SUM/COUNT), not the mean of monthly averages.
    Min/max/count over the same fact rows.

Where it is used
    pytest tests/test_aggregations.py.

When to use
    Add a case if a new additive agg is folded in collapse_pandas_detail.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute
from tests.conftest import make_context, write_yaml, value_of


@pytest.fixture
def agg_parquet(tmp_path):
    """Uneven row counts so weighted avg ≠ mean of monthly avgs."""
    rows = []
    # Jan: 10, 90 → monthly avg 50, count 2
    for amount in (10, 90):
        rows.append(_fact(date(2026, 1, 1), amount))
    # Feb: 20 → monthly avg 20, count 1
    rows.append(_fact(date(2026, 2, 1), 20))
    # Mar: 30 → monthly avg 30, count 1
    rows.append(_fact(date(2026, 3, 1), 30))
    path = tmp_path / "agg.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def test_avg_is_weighted_not_mean_of_monthly(agg_parquet, extra_config):
    """3m avg = 150/4 = 37.5, not (50+20+30)/3."""
    write_yaml(extra_config / "kpis" / "9010.yaml", _agg_kpi(9010))
    ctx = make_context(
        agg_parquet,
        measures=["avg_point", "avg_3m", "min_point", "max_3m", "count_3m", "min_3m"],
        supplier=["ABC"],
        kpi_id=9010,
    )
    result = compute(ctx, config_dir=extra_config)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert value_of(row, "avg_point") == 30.0
    assert value_of(row, "avg_3m") == 37.5
    assert value_of(row, "min_point") == 30.0
    assert value_of(row, "min_3m") == 10.0
    assert value_of(row, "max_3m") == 90.0
    assert value_of(row, "count_3m") == 4.0


def _fact(month: date, amount: float) -> dict:
    """One fact row for the aggregation fixture."""
    return {
        "event_month": month,
        "region": "NA",
        "reason_code": "LATE_SUPPLIER",
        "supplier_name": "ABC",
        "amount": amount,
    }


def _agg_kpi(kpi_id: int) -> dict:
    """Single global cut; several additive aggs on amount."""
    return {
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
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "default_dimensions": [],
        "base_measures": {
            "amount_avg": {"sql": "amount", "agg": "avg"},
            "amount_min": {"sql": "amount", "agg": "min"},
            "amount_max": {"sql": "amount", "agg": "max"},
            "amount_count": {"sql": "amount", "agg": "count"},
        },
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "avg_point": {"of": "amount_avg", "op": "point", "offset": {"months": 0}},
            "avg_3m": {
                "of": "amount_avg",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "min_point": {"of": "amount_min", "op": "point", "offset": {"months": 0}},
            "min_3m": {
                "of": "amount_min",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "max_3m": {
                "of": "amount_max",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
            "count_3m": {
                "of": "amount_count",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    }


def test_count_on_text_column_is_row_count(tmp_path, extra_config):
    """agg: count on a string id must count rows, not coerce to NaN and return 0."""
    rows = [
        _fact(date(2026, 3, 1), 10) | {"line_id": "L1"},
        _fact(date(2026, 3, 1), 20) | {"line_id": "L2"},
        _fact(date(2026, 3, 1), 0) | {"line_id": "L3"},
    ]
    path = tmp_path / "lines.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    spec = _agg_kpi(9011)
    spec["base_measures"] = {
        "line_rows": {"sql": "line_id", "agg": "count"},
        "billed_rows": {
            "sql": "CASE WHEN amount > 0 THEN line_id END",
            "agg": "count",
        },
    }
    spec["measures"] = {
        "lines": {"of": "line_rows", "op": "point", "offset": {"months": 0}},
        "billed": {"of": "billed_rows", "op": "point", "offset": {"months": 0}},
    }
    write_yaml(extra_config / "kpis" / "9011.yaml", spec)
    ctx = make_context(
        path,
        measures=["lines", "billed"],
        supplier=["ABC"],
        kpi_id=9011,
    )
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "line_id",
    ]
    result = compute(ctx, config_dir=extra_config)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["lines"] == pytest.approx(3.0)
    assert row["billed"] == pytest.approx(2.0)
