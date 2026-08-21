"""Non-additive aggregation tests: count_distinct, median, percentile.

What this file provides
    Window of count_distinct is distinct across the span, not the sum of
    monthly uniques. Median/percentile run on row-level amounts. Global
    distinct is not the sum of regional distincts.

Where it is used
    pytest tests/test_nonadditive_aggs.py.

When to use
    Add a case when a new non-additive agg is added to DuckDB/Pandas.
"""

import pandas as pd

from kpi_engine import compute
from tests.conftest import make_context, write_yaml


def test_count_distinct_window_is_not_sum_of_months(tmp_path, extra_config):
    """3m distinct suppliers is unique(ABC,XYZ,DEF)=3, not 2+1+2."""
    path = _facts(tmp_path)
    write_yaml(extra_config / "kpis" / "9031.yaml", _distinct_kpi(9031))
    ctx = make_context(
        path,
        measures=["distinct_now", "distinct_3m"],
        kpi_id=9031,
    )
    result = compute(ctx, config_dir=extra_config)
    na = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "R" and r["region"] == "NA"
    )
    assert na["distinct_now"] == 2.0
    assert na["distinct_3m"] == 3.0


def test_count_distinct_g_is_not_sum_of_r(tmp_path, extra_config):
    """A supplier present in two regions counts once on G."""
    path = _facts(tmp_path)
    write_yaml(extra_config / "kpis" / "9031.yaml", _distinct_kpi(9031))
    ctx = make_context(path, measures=["distinct_now"], kpi_id=9031)
    result = compute(ctx, config_dir=extra_config)
    g = next(r for r in result["rows"] if r["output_cut"] == "G")
    r_sum = sum(r["distinct_now"] for r in result["rows"] if r["output_cut"] == "R")
    # NA has ABC+XYZ (2), EU has ABC (1); G has ABC+XYZ (2), not 3
    assert g["distinct_now"] == 2.0
    assert r_sum == 3.0


def test_median_and_percentile_from_rows(tmp_path, extra_config):
    """Median of 10,20,30 is 20; p90 is interpolated from the same rows."""
    path = tmp_path / "med.parquet"
    pd.DataFrame(
        [
            _row("2026-03-01", "NA", 10),
            _row("2026-03-01", "NA", 20),
            _row("2026-03-01", "NA", 30),
        ]
    ).to_parquet(path, index=False)
    write_yaml(extra_config / "kpis" / "9035.yaml", _stat_kpi(9035))
    ctx = make_context(
        path, measures=["median_now", "p90_now"], supplier=["ABC"], kpi_id=9035
    )
    result = compute(ctx, config_dir=extra_config)
    row = result["rows"][0]
    assert row["median_now"] == 20.0
    assert row["p90_now"] == 28.0


def _facts(tmp_path):
    """NA has ABC+XYZ in March; DEF only in January; EU has ABC in March."""
    rows = [
        _row("2026-03-01", "NA", 1, "ABC"),
        _row("2026-03-01", "NA", 1, "XYZ"),
        _row("2026-03-01", "EU", 1, "ABC"),
        _row("2026-02-01", "NA", 1, "ABC"),
        _row("2026-01-01", "NA", 1, "ABC"),
        _row("2026-01-01", "NA", 1, "DEF"),
    ]
    path = tmp_path / "distinct.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _row(month, region, amount, supplier="ABC") -> dict:
    """One fact row for non-additive fixtures."""
    return {
        "event_month": month,
        "region": region,
        "reason_code": "LATE_SUPPLIER",
        "supplier_name": supplier,
        "amount": amount,
    }


def _distinct_kpi(kpi_id: int) -> dict:
    """count_distinct of supplier_name at point and 3m window, G+R."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
        },
        "dimensions": [{"name": "region", "kind": "dimension"}],
        "base_measures": {
            "n_suppliers": {"sql": "supplier_name", "agg": "count_distinct"}
        },
        "cuts": [
            {
                "name": "G",
                "group_by": [],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
        "default_cut": "G",
        "measures": {
            "distinct_now": {
                "of": "n_suppliers",
                "op": "point",
                "offset": {"months": 0},
            },
            "distinct_3m": {
                "of": "n_suppliers",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    }


def _stat_kpi(kpi_id: int) -> dict:
    """Median and 90th percentile of amount on a global cut."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
        },
        "dimensions": [{"name": "region", "kind": "dimension"}],
        "base_measures": {
            "amount_median": {"sql": "amount", "agg": "median"},
            "amount_p90": {"sql": "amount", "agg": "percentile", "percentile": 90},
        },
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": {
            "median_now": {
                "of": "amount_median",
                "op": "point",
                "offset": {"months": 0},
            },
            "p90_now": {"of": "amount_p90", "op": "point", "offset": {"months": 0}},
        },
    }
