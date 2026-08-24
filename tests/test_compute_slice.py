"""End-to-end slice tests: scan width, errors, UDF entry.

What this file provides
    Unrequested measures do not widen lookback; missing month filter errors;
    unknown measure_key errors; kpi_engine.main matches compute.

Where it is used
    pytest tests/test_compute_slice.py.

When to use
    Add a test here for a new request-level rule (pagination, extra view, etc.).
"""
from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError, TimePlanError
from tests.conftest import make_context
from kpi_engine.main import main


def test_unrequested_measures_do_not_widen_scan(parquet_path, config_dir):
    """Asking only for 3m must not scan 13 months just because previous_year exists in YAML."""
    three = validate(
        make_context(parquet_path, measures=["value_3m"], supplier=["ABC"]),
        config_dir=config_dir,
    )
    with_py = validate(
        make_context(
            parquet_path,
            measures=["value_3m", "previous_year_value"],
            supplier=["ABC"],
        ),
        config_dir=config_dir,
    )
    trend = validate(
        make_context(parquet_path, measures=["trend_12m"], supplier=["ABC"]),
        config_dir=config_dir,
    )
    assert three["lookback_months"] == 2
    assert with_py["lookback_months"] == 12
    assert trend["lookback_months"] == 11


def test_missing_month_filter_is_an_error(parquet_path, config_dir):
    """No selected month → TimePlanError; we do not default to latest data."""
    ctx = make_context(parquet_path, measures=["value_3m"])
    del ctx["filters"]["reporting_month"]
    try:
        validate(ctx, config_dir=config_dir)
    except TimePlanError as exc:
        assert "reporting_month" in str(exc)
    else:
        raise AssertionError("expected TimePlanError")


def test_unknown_measure_key(parquet_path, config_dir):
    """Context measure_key must exist in KPI YAML measures."""
    ctx = make_context(parquet_path, measures=["not_a_real_measure"])
    try:
        validate(ctx, config_dir=config_dir)
    except BindError as exc:
        assert "not_a_real_measure" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_udf_entry_matches_compute(parquet_path, config_dir):
    """kpi_engine.main is a pass-through around kpi_engine.compute."""
    import importlib

    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    mod = importlib.import_module("kpi_engine.main")
    assert mod.main is main
    assert main(ctx, config_dir=str(config_dir))["rows"] == compute(ctx, config_dir=config_dir)["rows"]
