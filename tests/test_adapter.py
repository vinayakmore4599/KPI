"""Adapter tests: context parsing rules.

What this file provides
    Tests for value vs values, single-view assertion, heir rejection.

Where it is used
    pytest tests/test_adapter.py — no DuckDB.

When to use
    Add a case here when the context envelope changes.
"""
from kpi_engine.core.adapter import adapt
from kpi_engine.exceptions import ContextError, FilterError
from tests.conftest import make_context


def test_accepts_value_and_values(parquet_path):
    """Filters may send `value` or `values`; both become IN lists."""
    ctx = make_context(parquet_path, measures=["current_value"], region=["NA"], supplier=["ABC"])
    request = adapt(ctx)
    codes = {f.code: f.values for f in request.filters}
    assert codes["region"] == ("NA",)
    assert codes["Supplier Name"] == ("ABC",)
    assert request.measure_keys == ("current_value",)


def test_measures_requested_is_the_host_alias_for_measures_required(parquet_path):
    """Metadata sends measures_requested; that list is the projection."""
    ctx = make_context(parquet_path, measures=[])
    view = ctx["execution"]["view_details"][0]
    view.pop("measures_required", None)
    view["measures requested"] = [
        {"measure_key": "current_value"},
        {"measure_key": "previous year_value"},
        {"MeasureKey": "value_3m"},
    ]
    request = adapt(ctx)
    assert request.measure_keys == ("current_value", "previous year_value", "value_3m")


def test_plain_string_measure_keys_are_accepted(parquet_path):
    """A host list of measure_key strings is the same as objects with measure_key."""
    ctx = make_context(parquet_path, measures=[])
    ctx["execution"]["view_details"][0]["measures_required"] = ["current_value", "value_6m"]
    assert adapt(ctx).measure_keys == ("current_value", "value_6m")


def test_rejects_multiple_views(parquet_path):
    """One KPI maps to one view; extra view_details entries fail."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"].append({"view_id": 2, "view_name": "x", "measures_required": []})
    try:
        adapt(ctx)
    except ContextError as exc:
        assert "exactly one view" in str(exc)
    else:
        raise AssertionError("expected ContextError")


def test_rejects_hierarchy_filters(parquet_path):
    """heir filters must be expanded upstream, not treated as a simple IN."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        extra_filters={"Supplier Name": {"value": ["ABC"], "input_text": "heir"}},
    )
    try:
        adapt(ctx)
    except FilterError as exc:
        assert "heir" in str(exc)
    else:
        raise AssertionError("expected FilterError")


def test_empty_measures_required_is_not_omitted(parquet_path):
    """Explicit [] is a projection of nothing, not a missing field."""
    ctx = make_context(parquet_path, measures=[])
    request = adapt(ctx)
    assert request.measure_keys == ()
    assert request.measures_omitted is False


def test_omitted_measures_required_is_flagged(parquet_path):
    """Host omitted the measures field so SelectedMetrics may apply later."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"][0].pop("measures_required", None)
    request = adapt(ctx)
    assert request.measure_keys == ()
    assert request.measures_omitted is True


def test_execution_time_grain_is_adapted(parquet_path):
    request = adapt(make_context(parquet_path, measures=["current_value"], time_grain="week"))
    assert request.time_grain == "week"


def test_unknown_time_grain_is_rejected(parquet_path):
    ctx = make_context(parquet_path, measures=["current_value"], time_grain="fortnight")
    try:
        adapt(ctx)
    except ContextError as exc:
        assert "time_grain" in str(exc)
    else:
        raise AssertionError("expected ContextError")
