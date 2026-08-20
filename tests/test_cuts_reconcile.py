from kpi_engine import compute
from tests.conftest import make_context


def test_additive_window_reconciles_without_region_filter(parquet_path, config_dir):
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
