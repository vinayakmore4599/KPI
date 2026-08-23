"""Context envelope rules: every shape the metadata layer can send.

What this file provides
    Negative and edge cases for adapter.adapt — execution block, single view,
    measures_required, filter normalization, datasets, mappings, pagination.

Where it is used
    pytest tests/test_context_validation.py — no DuckDB, no YAML.

When to use
    Add a case whenever the context JSON contract gains or loosens a field.
"""

import pytest

from kpi_engine.core.adapter import adapt
from kpi_engine.exceptions import ContextError, FilterError
from tests.conftest import make_context


def test_context_must_be_an_object():
    """A non-object context is rejected before anything else runs."""
    with pytest.raises(ContextError, match="Context must be a JSON object"):
        adapt("not a context")


def test_execution_block_is_required(parquet_path):
    """execution must be present and be an object."""
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["execution"]
    with pytest.raises(ContextError, match="execution must be an object"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"] = []
    with pytest.raises(ContextError, match="execution must be an object"):
        adapt(ctx)


def test_kpi_id_is_required(parquet_path):
    """Without execution.kpi_id the engine cannot pick a KPI YAML."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["kpi_id"] = None
    with pytest.raises(ContextError, match="execution.kpi_id is required"):
        adapt(ctx)


def test_view_details_must_hold_exactly_one_view(parquet_path):
    """One KPI maps to one view: zero, many, or non-list view_details all fail."""
    for value in ([], "views", None):
        ctx = make_context(parquet_path, measures=["current_value"])
        ctx["execution"]["view_details"] = value
        with pytest.raises(ContextError, match="exactly one view"):
            adapt(ctx)


def test_view_entry_must_be_an_object(parquet_path):
    """view_details[0] must be an object, not a bare id."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"] = [13]
    with pytest.raises(ContextError, match="view_details\\[0\\] must be an object"):
        adapt(ctx)


def test_measures_required_may_be_omitted(parquet_path):
    """A view with no measures_required yields an empty projection, not an error."""
    ctx = make_context(parquet_path, measures=[])
    ctx["execution"]["view_details"][0]["measures_required"] = None
    request = adapt(ctx)
    assert request.measure_keys == ()
    assert request.measures_omitted is True


def test_measures_required_shape_is_validated(parquet_path):
    """measures_required must be a list of objects carrying a non-empty measure_key."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"][0]["measures_required"] = "current_value"
    with pytest.raises(ContextError, match="measures_required must be a list"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"][0]["measures_required"] = [{"name": "current_value"}]
    with pytest.raises(ContextError, match="needs measure_key"):
        adapt(ctx)

    for bad_key in ("", 7):
        ctx = make_context(parquet_path, measures=["current_value"])
        ctx["execution"]["view_details"][0]["measures_required"] = [{"measure_key": bad_key}]
        with pytest.raises(ContextError, match="Invalid measure_key"):
            adapt(ctx)


def test_measure_key_order_and_duplicates_are_preserved(parquet_path):
    """The projection list is passed through verbatim; the engine does not reorder it."""
    ctx = make_context(parquet_path, measures=["value_3m", "current_value", "value_3m"])
    assert adapt(ctx).measure_keys == ("value_3m", "current_value", "value_3m")


def test_filters_block_shape_is_validated(parquet_path):
    """filters must be an object of objects."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["filters"] = ["region"]
    with pytest.raises(ContextError, match="filters must be an object"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["filters"]["region"] = "NA"
    with pytest.raises(ContextError, match="filters\\['region'\\] must be an object"):
        adapt(ctx)


def test_scalar_and_missing_filter_values_normalize_to_tuples(parquet_path):
    """A scalar becomes a one-element IN list; a filter with no value becomes empty."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        extra_filters={
            "region": {"value": "NA", "input_text": "simple"},
            "reason_code": {"input_text": "simple"},
        },
    )
    by_code = {f.code: f for f in adapt(ctx).filters}
    assert by_code["region"].values == ("NA",)
    assert by_code["reason_code"].values == ()


def test_values_wins_over_value_when_both_present(parquet_path):
    """`values` is the canonical key; `value` is the legacy alias."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        extra_filters={"region": {"values": ["EU"], "value": ["NA"], "input_text": "simple"}},
    )
    by_code = {f.code: f for f in adapt(ctx).filters}
    assert by_code["region"].values == ("EU",)


def test_hierarchy_filter_is_rejected_for_any_filter(parquet_path):
    """input_text=heir must be expanded upstream, whichever filter carries it."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        extra_filters={"region": {"value": ["EMEA"], "input_text": "heir"}},
    )
    with pytest.raises(FilterError, match="hierarchical"):
        adapt(ctx)


def test_datasets_block_shape_is_validated(parquet_path):
    """datasets must be a non-empty object of objects with list columns."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"] = {}
    with pytest.raises(ContextError, match="datasets must be a non-empty object"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"]["Sotif"] = str(parquet_path)
    with pytest.raises(ContextError, match="datasets\\['Sotif'\\] must be an object"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"]["Sotif"]["columns"] = "amount"
    with pytest.raises(ContextError, match="columns must be a list"):
        adapt(ctx)


def test_dataset_defaults_alias_to_key_and_path_to_blank(parquet_path):
    """Missing alias falls back to the dataset key; missing path stays blank for the binder."""
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["datasets"]["Sotif"]["alias"]
    del ctx["datasets"]["Sotif"]["path"]
    dataset = adapt(ctx).datasets[0]
    assert dataset.key == "Sotif"
    assert dataset.alias == "Sotif"
    assert dataset.path == ""
    assert dataset.table_type == "PARQUET"


def test_filter_mappings_are_validated_and_normalized(parquet_path):
    """Mapping entries need filter_code and column_name; operator defaults to lowercase in."""
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"]["Sotif"]["filter_column_mappings"] = ["region"]
    with pytest.raises(ContextError, match="filter_column_mappings entries must be objects"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"]["Sotif"]["filter_column_mappings"] = [{"filter_code": "region"}]
    with pytest.raises(ContextError, match="need filter_code and column_name"):
        adapt(ctx)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["datasets"]["Sotif"]["filter_column_mappings"] = [
        {"filter_code": "region", "column_name": "region", "operator": "IN"},
        {"filter_code": "plant", "column_name": "region"},
    ]
    mappings = adapt(ctx).datasets[0].mappings
    assert [m.operator for m in mappings] == ["in", "in"]


def test_pagination_defaults_and_coercion(parquet_path):
    """Missing output, blank strings, and numeric strings all normalize predictably."""
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["output"]
    empty = adapt(ctx).pagination
    assert (empty.page, empty.page_size, empty.limit) == (None, None, None)

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["output"] = "pagination"
    assert adapt(ctx).pagination.page is None

    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["output"] = {"page": "2", "page_size": "", "limit": 5}
    paging = adapt(ctx).pagination
    assert (paging.page, paging.page_size, paging.limit) == (2, None, 5)


def test_request_id_is_optional(parquet_path):
    """request_id is metadata only; omitting it must not fail the request."""
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["execution"]["request_id"]
    assert adapt(ctx).request_id is None


def test_adapt_does_not_mutate_the_caller_context(parquet_path):
    """The context is immutable input: adapt reads it and keeps a reference, never edits it."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    before = repr(ctx)
    request = adapt(ctx)
    assert repr(ctx) == before
    assert request.raw is ctx
