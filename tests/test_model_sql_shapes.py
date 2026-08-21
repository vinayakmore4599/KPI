"""Generated DuckDB SQL: scans, aggregates, time buckets, and parameters.

What this file provides
    Compile-level assertions on the extract SQL — quoting, parameter binding,
    scan function choice, GROUP BY shape, row-level detail queries — plus the
    failure modes (unbound source, missing join, DuckDB error).

Where it is used
    pytest tests/test_model_sql_shapes.py.

When to use
    Add a case when model_sql changes how it builds SQL. Assert on shape here
    and on numbers in the calculation tests.
"""

from datetime import date

import duckdb
import pytest

from kpi_engine import validate
from kpi_engine.contracts import DatasetBinding, TimePlan
from kpi_engine.core.adapter import adapt
from kpi_engine.core.binder import bind_datasets, load_kpi, load_model
from kpi_engine.core.model_sql import compile_extract, extract
from kpi_engine.exceptions import BindError, KPIEngineError
from tests.conftest import make_context, minimal_kpi, write_yaml

_AGG_MEASURES = {
    "sum_value": {"sql": "amount", "agg": "sum"},
    "count_value": {"sql": "amount", "agg": "count"},
    "min_value": {"sql": "amount", "agg": "min"},
    "max_value": {"sql": "amount", "agg": "max"},
    "avg_value": {"sql": "amount", "agg": "avg"},
}


def test_every_additive_agg_compiles_to_its_sql_function(parquet_path, extra_config):
    """sum/count/min/max map one-to-one; avg is carried as SUM plus COUNT."""
    write_yaml(
        extra_config / "kpis" / "9500.yaml",
        minimal_kpi(
            9500,
            base_measures=_AGG_MEASURES,
            measures={"current_value": {"of": "sum_value", "op": "point", "offset": {"months": 0}}},
        ),
    )
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9500)
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert 'SUM("amount") AS "sum_value"' in sql
    assert 'COUNT("amount") AS "count_value"' in sql
    assert 'MIN("amount") AS "min_value"' in sql
    assert 'MAX("amount") AS "max_value"' in sql
    assert 'SUM("amount") AS "avg_value__sum"' in sql
    assert 'COUNT("amount") AS "avg_value__count"' in sql


def test_additive_extract_groups_by_grain_ordinals(parquet_path, config_dir):
    """GROUP BY lists one ordinal per grain column, so dims and time stay aligned."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    sql = validate(ctx, config_dir=config_dir)["sql"]
    assert sql.rstrip().endswith("GROUP BY 1, 2, 3")
    assert '"reason_code"' in sql
    assert '"region"' in sql


def test_non_additive_measures_compile_to_a_row_level_query(parquet_path, extra_config):
    """count_distinct cannot be pre-aggregated, so its extract has no GROUP BY."""
    write_yaml(
        extra_config / "kpis" / "9501.yaml",
        minimal_kpi(
            9501,
            base_measures={"distinct_suppliers": {"sql": "supplier_name", "agg": "count_distinct"}},
            measures={
                "current_value": {
                    "of": "distinct_suppliers",
                    "op": "point",
                    "offset": {"months": 0},
                }
            },
        ),
    )
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9501)
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert "GROUP BY" not in sql
    assert '"supplier_name"' in sql


def test_mixed_aggs_compile_to_one_query_per_shape(parquet_path, extra_config):
    """Additive and non-additive measures need separate extracts from the same model."""
    write_yaml(
        extra_config / "kpis" / "9502.yaml",
        minimal_kpi(
            9502,
            base_measures={
                "sotif_value": {"sql": "amount", "agg": "sum"},
                "distinct_suppliers": {"sql": "supplier_name", "agg": "count_distinct"},
            },
            measures={
                "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
                "supplier_count": {
                    "of": "distinct_suppliers",
                    "op": "point",
                    "offset": {"months": 0},
                },
            },
        ),
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value", "supplier_count"],
        supplier=["ABC"],
        kpi_id=9502,
    )
    planned = validate(ctx, config_dir=extra_config)
    assert len(planned["sqls"]) == 2
    grouped, row_level = planned["sqls"]
    assert "GROUP BY" in grouped
    assert "GROUP BY" not in row_level


@pytest.mark.parametrize(
    ("grain", "expected"),
    [
        ("day", "date_trunc('day'"),
        ("month", "date_trunc('month'"),
        ("quarter", "date_trunc('quarter'"),
        ("year", "date_trunc('year'"),
    ],
)
def test_time_bucket_uses_the_kpi_grain(parquet_path, extra_config, grain, expected):
    """The extract truncates the time column in SQL, not in pandas."""
    month = "2026-03-17" if grain == "day" else "2026-03"
    kpi_id = {"day": 9503, "month": 9504, "quarter": 9505, "year": 9506}[grain]
    write_yaml(
        extra_config / "kpis" / f"{kpi_id}.yaml",
        minimal_kpi(
            kpi_id,
            time={
                "column": "event_month",
                "grain": grain,
                "filter_code": "reporting_month",
                "calendar": "gregorian",
            },
        ),
    )
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], month=month, kpi_id=kpi_id
    )
    assert expected in validate(ctx, config_dir=extra_config)["sql"]


@pytest.mark.parametrize(("grain", "kpi_id"), [("quarter", 9507), ("year", 9508)])
def test_fiscal_calendar_shifts_the_bucket_in_sql(parquet_path, extra_config, grain, kpi_id):
    """A fiscal year starting in April becomes an INTERVAL shift around date_trunc."""
    write_yaml(
        extra_config / "kpis" / f"{kpi_id}.yaml",
        minimal_kpi(
            kpi_id,
            time={
                "column": "event_month",
                "grain": grain,
                "filter_code": "reporting_month",
                "calendar": "fiscal",
                "fiscal_start_month": 4,
            },
        ),
    )
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=kpi_id)
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert "- INTERVAL 3 MONTH" in sql
    assert "+ INTERVAL 3 MONTH" in sql


def test_filter_values_and_paths_are_bound_as_parameters(parquet_path, config_dir):
    """No user value is ever concatenated into SQL text."""
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC", "XYZ"], region=["NA"]
    )
    planned = validate(ctx, config_dir=config_dir)
    sql = planned["sql"]
    assert "ABC" not in sql
    assert "XYZ" not in sql
    assert str(parquet_path) not in sql
    # path + span start + span end + two supplier values (region is deferred to the R cut)
    assert planned["param_count"] == 5
    assert '"supplier_name" IN (?, ?)' in sql


def test_delta_tables_use_delta_scan_and_parquet_files_do_not(parquet_path, config_dir):
    """table_type DELTA scans a Delta table unless the path is a parquet file."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    ctx["datasets"]["Sotif"]["table_type"] = "DELTA"
    ctx["datasets"]["Sotif"]["path"] = "abfss://command@account.dfs.core.windows.net/sotif"
    assert "delta_scan(?)" in validate(ctx, config_dir=config_dir)["sql"]

    ctx["datasets"]["Sotif"]["path"] = str(parquet_path)
    assert "read_parquet(?)" in validate(ctx, config_dir=config_dir)["sql"]


def test_single_alias_model_without_sources_still_scans(parquet_path, extra_config):
    """A model declaring only required_aliases scans that alias directly."""
    write_yaml(
        extra_config / "models" / "alias_only.yaml",
        {"model_id": "alias_only", "kind": "physical", "required_aliases": ["sotif"]},
    )
    write_yaml(extra_config / "kpis" / "9509.yaml", minimal_kpi(9509, model="alias_only"))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9509)
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert 'read_parquet(?) AS "sotif"' in sql


def test_second_source_without_a_join_is_rejected(parquet_path, extra_config, tmp_path):
    """Two physical sources need an explicit join; the engine will not guess keys."""
    regions = tmp_path / "regions.parquet"
    import pandas as pd

    pd.DataFrame([{"region": "NA"}]).to_parquet(regions, index=False)
    write_yaml(
        extra_config / "models" / "no_join.yaml",
        {
            "model_id": "no_join",
            "kind": "physical",
            "sources": {"sotif": {"alias": "sotif"}, "regions": {"alias": "regions"}},
            "joins": [],
        },
    )
    write_yaml(extra_config / "kpis" / "9510.yaml", minimal_kpi(9510, model="no_join"))
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9510,
        extra_datasets={
            "Regions": {
                "dataset_id": 22,
                "table_type": "PARQUET",
                "path": str(regions),
                "alias": "regions",
                "columns": ["region"],
                "filter_column_mappings": [],
            }
        },
    )
    with pytest.raises(BindError, match="Source regions has no join"):
        validate(ctx, config_dir=extra_config)


def test_unbound_source_alias_is_reported(parquet_path, extra_config):
    """compile_extract refuses to scan a source that has no dataset binding."""
    write_yaml(
        extra_config / "models" / "two_source.yaml",
        {
            "model_id": "two_source",
            "kind": "physical",
            "sources": {"sotif": {"alias": "sotif"}, "regions": {"alias": "regions"}},
            "joins": [{"left": "sotif", "right": "regions", "on": ["region"], "type": "inner"}],
        },
    )
    write_yaml(extra_config / "kpis" / "9511.yaml", minimal_kpi(9511, model="two_source"))
    kpi = load_kpi(9511, extra_config)
    model = load_model("two_source", extra_config)
    request = adapt(make_context(parquet_path, measures=["current_value"], kpi_id=9511))
    only_facts = {
        "sotif": DatasetBinding(
            key="Sotif",
            alias="sotif",
            path=str(parquet_path),
            table_type="PARQUET",
            columns=("event_month", "region", "amount"),
            mappings=(),
        )
    }
    assert request.kpi_id == 9511
    with pytest.raises(BindError, match="No dataset bound for source regions"):
        compile_extract(
            model=model,
            kpi=kpi,
            datasets=only_facts,
            source_filters=(),
            plan=_plan(),
            grain=("event_month", "reason_code", "region"),
        )


def test_grouped_select_omits_non_additive_measures(parquet_path, extra_config):
    """DuckDB never pre-aggregates a median, so the grouped SELECT carries grain only."""
    write_yaml(
        extra_config / "kpis" / "9512.yaml",
        minimal_kpi(
            9512,
            base_measures={"median_amount": {"sql": "amount", "agg": "median"}},
            measures={
                "current_value": {"of": "median_amount", "op": "point", "offset": {"months": 0}}
            },
        ),
    )
    kpi = load_kpi(9512, extra_config)
    model = load_model("sotif", extra_config)
    request = adapt(make_context(parquet_path, measures=["current_value"], kpi_id=9512))
    grouped, _params = compile_extract(
        model=model,
        kpi=kpi,
        datasets=bind_datasets(model, request),
        source_filters=(),
        plan=_plan(),
        grain=("event_month",),
        row_level=False,
    )
    assert "median" not in grouped.lower()
    assert "MEDIAN(" not in grouped
    assert grouped.startswith("SELECT CAST(date_trunc('month'")


def test_duckdb_failures_surface_as_engine_errors(parquet_path, extra_config):
    """A bad scan is reported as a KPI engine error, not a raw DuckDB traceback."""
    kpi = load_kpi(3004, extra_config)
    model = load_model("sotif", extra_config)
    request = adapt(make_context(parquet_path, measures=["current_value"]))
    datasets = bind_datasets(model, request)
    broken = {
        "sotif": DatasetBinding(
            key="Sotif",
            alias="sotif",
            path="/nonexistent/path/to/missing.parquet",
            table_type="PARQUET",
            columns=datasets["sotif"].columns,
            mappings=(),
        )
    }
    with pytest.raises(KPIEngineError, match="DuckDB extract failed"):
        extract(
            model=model,
            kpi=kpi,
            datasets=broken,
            source_filters=(),
            plan=_plan(),
            grain=("event_month", "reason_code", "region"),
        )


def test_extract_reuses_a_caller_supplied_connection(parquet_path, extra_config):
    """A shared DuckDB connection is used and left open for the rest of the request."""
    kpi = load_kpi(3004, extra_config)
    model = load_model("sotif", extra_config)
    request = adapt(make_context(parquet_path, measures=["current_value"]))
    connection = duckdb.connect()
    try:
        result = extract(
            model=model,
            kpi=kpi,
            datasets=bind_datasets(model, request),
            source_filters=(),
            plan=_plan(),
            grain=("event_month", "reason_code", "region"),
            connection=connection,
        )
        assert not result.frame.empty
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


def test_filters_that_match_nothing_return_an_empty_extract(parquet_path, config_dir):
    """A supplier that does not exist yields no rows rather than an error."""
    from kpi_engine import compute

    ctx = make_context(parquet_path, measures=["current_value"], supplier=["DOES_NOT_EXIST"])
    result = compute(ctx, config_dir=config_dir)
    assert result["rows"] == []
    assert result["pagination"]["total_count"] == 0


def test_anchor_month_in_filter_is_caught_by_the_compile_guard():
    """The locked rule "the selected month is a range" is enforced on generated SQL."""
    from kpi_engine.core.model_sql import _assert_no_month_in

    plan = _plan()
    ranged = 'SELECT 1 FROM t WHERE "event_month" >= ? AND "event_month" < ?'
    _assert_no_month_in(ranged, plan)

    with pytest.raises(KPIEngineError, match="must not IN-filter the anchor month"):
        _assert_no_month_in("SELECT 1 FROM t WHERE event_month IN ('2026-03-01')", plan)


def _plan() -> TimePlan:
    """Fixed March 2026 anchor with a two-month lookback."""
    return TimePlan(
        anchor=date(2026, 3, 1),
        span_start=date(2026, 1, 1),
        span_end_exclusive=date(2026, 4, 1),
        lookback_months=2,
        claimed_filter_code="reporting_month",
    )
