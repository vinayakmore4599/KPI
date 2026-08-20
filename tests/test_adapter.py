from kpi_engine.core.adapter import adapt
from kpi_engine.exceptions import ContextError, FilterError
from tests.conftest import make_context


def test_accepts_value_and_values(parquet_path):
    ctx = make_context(parquet_path, measures=["current_value"], region=["NA"], supplier=["ABC"])
    request = adapt(ctx)
    codes = {f.code: f.values for f in request.filters}
    assert codes["region"] == ("NA",)
    assert codes["Supplier Name"] == ("ABC",)
    assert request.measure_keys == ("current_value",)


def test_rejects_multiple_views(parquet_path):
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"].append({"view_id": 2, "view_name": "x", "measures_required": []})
    try:
        adapt(ctx)
    except ContextError as exc:
        assert "exactly one view" in str(exc)
    else:
        raise AssertionError("expected ContextError")


def test_rejects_hierarchy_filters(parquet_path):
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
