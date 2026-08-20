"""Cut tests: G vs R grouping and ignore_filters.

What this file provides
    Additive window: G value_3m equals sum of R rows without a region filter.
    With region=NA, G stays worldwide; R is NA only.

Where it is used
    pytest tests/test_cuts_reconcile.py.

When to use
    Add a case when introducing a new cut or ignore_filters behaviour.
"""
from kpi_engine import compute
from tests.conftest import make_context


def test_additive_window_reconciles_without_region_filter(parquet_path, config_dir):
    """For sum, global value_3m equals the sum of regional value_3m rows."""
    ctx = make_context(
        parquet_path,
        measures=["value_3m", "reason_code"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir)
    g = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "G" and r["reason_code"] == "LATE_SUPPLIER"
    )
    r_sum = sum(
        r["value_3m"]
        for r in result["rows"]
        if r["output_cut"] == "R" and r["reason_code"] == "LATE_SUPPLIER"
    )
    assert g["value_3m"] == r_sum
    # Jan+Feb+Mar 2026: NA 10*(1+2+3) + EU 5*(1+2+3) = 60 + 30 = 90
    assert g["value_3m"] == 90.0


def test_region_filter_applies_only_to_r_when_g_ignores_it(parquet_path, config_dir):
    """G ignores region so it stays worldwide; R still applies the region IN list."""
    ctx = make_context(
        parquet_path,
        measures=["value_3m"],
        supplier=["ABC"],
        region=["NA"],
    )
    result = compute(ctx, config_dir=config_dir)
    g = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "G" and r["reason_code"] == "LATE_SUPPLIER"
    )
    r_regions = {
        r["region"]
        for r in result["rows"]
        if r["output_cut"] == "R" and r["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}
    assert g["value_3m"] == 90.0
    na = next(
        r
        for r in result["rows"]
        if r["output_cut"] == "R" and r["region"] == "NA" and r["reason_code"] == "LATE_SUPPLIER"
    )
    assert na["value_3m"] == 60.0
