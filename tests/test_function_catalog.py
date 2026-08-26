"""Named function catalog: op: on columns, fn: + inputs: on measures.

What this file provides
    A custom registered column function called with declared columns, a fn
    measure consuming two other measures, bind-time rejection of unknown names
    and dependency cycles, and memoized diamond dependencies.

Where it is used
    pytest tests/test_function_catalog.py.

When to use
    Add a case when a new built-in is registered or the graph rules change.
"""

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.pipeline.fn_apply import (
    COLUMN_FNS,
    MEASURE_FNS,
    register_column_fn,
    register_measure_fn,
    unregister_column_fn,
    unregister_measure_fn,
)
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, unwrap_cell, value_of


@pytest.fixture
def weighted_fn():
    """Register a custom column function for one test, then remove it."""
    register_column_fn(
        "weighted_score",
        lambda hits, weight: hits * weight * 10,
        min_columns=2,
    )
    yield "weighted_score"
    unregister_column_fn("weighted_score")


@pytest.fixture
def safe_ratio_fn():
    """Register a custom measure function for one test, then remove it."""

    def safe_ratio(numerator, denominator):
        """Ratio that reports null instead of dividing by zero."""
        if numerator is None or not denominator:
            return None
        return float(numerator) / float(denominator)

    register_measure_fn("safe_ratio", safe_ratio)
    yield "safe_ratio"
    unregister_measure_fn("safe_ratio")


def _two_column_parquet(tmp_path, rows) -> str:
    """Fact table with ontime / fullqty / bonus columns."""
    frame = pd.DataFrame(rows)
    path = tmp_path / "fns.parquet"
    frame.to_parquet(path, index=False)
    return path


def _row(month="2026-03-01", ontime=1, fullqty=10, bonus=100) -> dict:
    """One NA / LATE_SUPPLIER fact row."""
    return {
        "event_month": month,
        "region": "NA",
        "reason_code": "LATE_SUPPLIER",
        "supplier_name": "ABC",
        "ontime": ontime,
        "fullqty": fullqty,
        "bonus": bonus,
    }


def _context(path, extra_config, kpi_id, measures):
    """Context pointing at the fact parquet."""
    ctx = make_context(path, measures=measures, supplier=["ABC"], kpi_id=kpi_id)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "ontime",
        "fullqty",
        "bonus",
    ]
    return ctx


def _column_op_kpi(kpi_id, base, measures=None):
    """A KPI whose single measure reads a base fact built by a column op."""
    spec = minimal_kpi(
        kpi_id,
        measures=measures
        or {"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
    )
    spec["base_measures"] = {"sotif_value": base}
    return spec


def _only_value(path, extra_config, kpi_id, measure="current_value"):
    """Compute the KPI and return the NA / LATE_SUPPLIER reason-cut value."""
    row = find_row(
        compute(_context(path, extra_config, kpi_id, [measure]), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    return unwrap_cell(row[measure])


def test_builtin_ops_are_registered():
    """Both registries carry the plain names and the older aliases."""
    columns = COLUMN_FNS
    measures = MEASURE_FNS
    assert {"value", "sum", "subtract", "multiply", "divide", "min", "max", "avg"} <= set(columns)
    assert {"sum", "subtract", "multiply", "divide", "percent", "growth_pct"} <= set(measures)
    assert {"identity", "mul", "add", "sub", "div"} <= set(columns)
    assert {"growth_pct", "div", "percent", "add", "sub", "mul"} <= set(measures)
    assert columns["mul"] is columns["multiply"]
    assert measures["yoy"] is measures["growth_pct"]


def test_custom_column_function_receives_declared_columns(tmp_path, extra_config, weighted_fn):
    """columns: [ontime, fullqty] are passed to the registered function as Series."""
    path = _two_column_parquet(tmp_path, [_row(ontime=2, fullqty=3)])
    spec = minimal_kpi(
        9901,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "sotif_value": {"columns": ["ontime", "fullqty"], "op": weighted_fn}
    }
    write_yaml(extra_config / "kpis" / "9901.yaml", spec)
    row = find_row(
        compute(_context(path, extra_config, 9901, ["current_value"]), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 60.0  # 2 * 3 * 10


def test_custom_column_function_folds_multiple_rows(tmp_path, extra_config, weighted_fn):
    """The function runs per row; agg folds the results."""
    path = _two_column_parquet(
        tmp_path, [_row(ontime=1, fullqty=1), _row(ontime=2, fullqty=2)]
    )
    spec = minimal_kpi(
        9902,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "sotif_value": {"columns": ["ontime", "fullqty"], "op": weighted_fn, "agg": "sum"}
    }
    write_yaml(extra_config / "kpis" / "9902.yaml", spec)
    row = find_row(
        compute(_context(path, extra_config, 9902, ["current_value"]), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert value_of(row, "current_value") == 50.0  # 1*1*10 + 2*2*10


def test_unknown_column_op_is_rejected_at_bind(extra_config):
    """An op that is not registered names the registry in the error."""
    spec = minimal_kpi(9903)
    spec["base_measures"] = {
        "sotif_value": {"columns": ["ontime", "fullqty"], "op": "no_such_fn"}
    }
    write_yaml(extra_config / "kpis" / "9903.yaml", spec)
    with pytest.raises(BindError, match="unknown op 'no_such_fn'"):
        load_kpi(9903, extra_config)


def test_column_op_arity_is_enforced_at_bind(extra_config, weighted_fn):
    """A function registered with min_columns=2 rejects a single column."""
    spec = minimal_kpi(9904)
    spec["base_measures"] = {"sotif_value": {"columns": ["ontime"], "op": weighted_fn}}
    write_yaml(extra_config / "kpis" / "9904.yaml", spec)
    with pytest.raises(BindError, match="needs at least 2 columns"):
        load_kpi(9904, extra_config)


def test_variadic_column_op_takes_as_many_columns_as_it_is_given(tmp_path, extra_config):
    """A `*columns` function has no upper bound, so three columns is as valid as two."""
    path = _two_column_parquet(tmp_path, [_row(ontime=2, fullqty=3, bonus=5)])
    spec = _column_op_kpi(9930, {"columns": ["ontime", "fullqty", "bonus"], "op": "sum"})
    write_yaml(extra_config / "kpis" / "9930.yaml", spec)
    assert _only_value(path, extra_config, 9930) == 10.0


def test_custom_variadic_column_function_is_not_capped_at_two(tmp_path, extra_config):
    """A registered `*columns` function is handed every declared column."""

    def count_hits(*columns):
        """How many columns arrived, as a Series aligned to the frame."""
        return columns[0] * 0 + len(columns)

    register_column_fn("count_hits", count_hits)
    try:
        path = _two_column_parquet(tmp_path, [_row()])
        spec = _column_op_kpi(
            9931, {"columns": ["ontime", "fullqty", "bonus"], "op": "count_hits"}
        )
        write_yaml(extra_config / "kpis" / "9931.yaml", spec)
        assert _only_value(path, extra_config, 9931) == 3.0
    finally:
        unregister_column_fn("count_hits")


def test_named_columns_bind_by_parameter_not_by_order(tmp_path, extra_config):
    """A {parameter: column} mapping reaches the right argument whatever the key order."""
    path = _two_column_parquet(tmp_path, [_row(ontime=2, fullqty=8)])
    spec = _column_op_kpi(
        9932,
        {"columns": {"denominator": "ontime", "numerator": "fullqty"}, "op": "divide"},
    )
    write_yaml(extra_config / "kpis" / "9932.yaml", spec)
    assert _only_value(path, extra_config, 9932) == 4.0  # fullqty / ontime, not 0.25


def test_row_wise_min_and_max_span_the_listed_columns(tmp_path, extra_config):
    """min / max reduce across the columns of one row, not down the rows."""
    path = _two_column_parquet(tmp_path, [_row(ontime=2, fullqty=8, bonus=5)])
    for kpi_id, op, expected in ((9933, "min", 2.0), (9934, "max", 8.0)):
        spec = _column_op_kpi(kpi_id, {"columns": ["ontime", "fullqty", "bonus"], "op": op})
        write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
        assert _only_value(path, extra_config, kpi_id) == expected


def test_too_many_columns_for_a_fixed_signature_fails_at_bind(extra_config):
    """divide takes exactly two columns, and says so with its parameter names."""
    spec = _column_op_kpi(9935, {"columns": ["ontime", "fullqty", "bonus"], "op": "divide"})
    write_yaml(extra_config / "kpis" / "9935.yaml", spec)
    with pytest.raises(BindError, match="takes at most 2 columns"):
        load_kpi(9935, extra_config)


def test_unknown_parameter_name_fails_at_bind(extra_config):
    """A mistyped parameter is caught and the real ones are listed."""
    spec = _column_op_kpi(
        9936, {"columns": {"numerator": "fullqty", "denom": "ontime"}, "op": "divide"}
    )
    write_yaml(extra_config / "kpis" / "9936.yaml", spec)
    with pytest.raises(BindError, match="has no parameter 'denom'"):
        load_kpi(9936, extra_config)


def test_variadic_op_cannot_be_called_with_parameter_names(extra_config):
    """sum takes any number of columns, so there is nothing to bind names to."""
    spec = _column_op_kpi(9937, {"columns": {"a": "ontime", "b": "fullqty"}, "op": "sum"})
    write_yaml(extra_config / "kpis" / "9937.yaml", spec)
    with pytest.raises(BindError, match="cannot be called with parameter names"):
        load_kpi(9937, extra_config)


def test_named_columns_without_an_op_fail_at_bind(extra_config):
    """Parameter names are meaningless with no function to pass them to."""
    spec = _column_op_kpi(9938, {"columns": {"numerator": "fullqty"}})
    write_yaml(extra_config / "kpis" / "9938.yaml", spec)
    with pytest.raises(BindError, match="names its columns but has no `op:`"):
        load_kpi(9938, extra_config)


def test_column_function_must_return_a_series(tmp_path, extra_config):
    """A function returning a scalar is a catalog error, not a broadcast surprise."""
    register_column_fn("bad_shape", lambda a, b: 1.0, min_columns=2)
    try:
        path = _two_column_parquet(tmp_path, [_row()])
        spec = minimal_kpi(
            9905,
            measures={
                "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            },
        )
        spec["base_measures"] = {
            "sotif_value": {"columns": ["ontime", "fullqty"], "op": "bad_shape"}
        }
        write_yaml(extra_config / "kpis" / "9905.yaml", spec)
        ctx = _context(path, extra_config, 9905, ["current_value"])
        with pytest.raises(CatalogError, match="must return a pandas Series"):
            compute(ctx, config_dir=extra_config)
    finally:
        unregister_column_fn("bad_shape")


def test_fn_measure_consumes_two_other_measures(tmp_path, extra_config, safe_ratio_fn):
    """inputs: [a, b] computes a and b first, then passes their scalars in."""
    path = _two_column_parquet(tmp_path, [_row(ontime=3, fullqty=12)])
    spec = minimal_kpi(9906)
    spec["base_measures"] = {
        "ontime_fact": {"sql": "ontime", "agg": "sum"},
        "total_fact": {"sql": "fullqty", "agg": "sum"},
    }
    spec["measures"] = {
        "ontime_value": {"of": "ontime_fact", "op": "point", "offset": {"months": 0}},
        "total_value": {"of": "total_fact", "op": "point", "offset": {"months": 0}},
        "otd_pct": {
            "op": "fn",
            "fn": safe_ratio_fn,
            "inputs": ["ontime_value", "total_value"],
        },
    }
    write_yaml(extra_config / "kpis" / "9906.yaml", spec)
    row = find_row(
        compute(_context(path, extra_config, 9906, ["otd_pct"]), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["otd_pct"] == pytest.approx(0.25)


def test_fn_measure_can_depend_on_another_fn_measure(tmp_path, extra_config, safe_ratio_fn):
    """A chain of fn measures resolves depth first."""
    path = _two_column_parquet(tmp_path, [_row(ontime=3, fullqty=12)])
    spec = minimal_kpi(9907)
    spec["base_measures"] = {
        "ontime_fact": {"sql": "ontime", "agg": "sum"},
        "total_fact": {"sql": "fullqty", "agg": "sum"},
    }
    spec["measures"] = {
        "ontime_value": {"of": "ontime_fact", "op": "point", "offset": {"months": 0}},
        "total_value": {"of": "total_fact", "op": "point", "offset": {"months": 0}},
        "otd_pct": {
            "op": "fn",
            "fn": safe_ratio_fn,
            "inputs": ["ontime_value", "total_value"],
        },
        "otd_scaled": {"op": "fn", "fn": "mul", "inputs": ["otd_pct", "total_value"]},
    }
    write_yaml(extra_config / "kpis" / "9907.yaml", spec)
    row = find_row(
        compute(_context(path, extra_config, 9907, ["otd_scaled"]), config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["otd_scaled"] == pytest.approx(3.0)  # 0.25 * 12


def test_diamond_dependency_is_computed_once(tmp_path, extra_config):
    """A measure named by two parents is evaluated a single time per combo."""
    calls: list[str] = []

    def counting_add(left, right):
        """Add, recording every call so the memo can be observed."""
        calls.append("add")
        if left is None or right is None:
            return None
        return float(left) + float(right)

    register_measure_fn("counting_add", counting_add)
    try:
        path = _two_column_parquet(tmp_path, [_row(ontime=1, fullqty=10)])
        spec = minimal_kpi(9908)
        spec["base_measures"] = {"sotif_value": {"sql": "fullqty", "agg": "sum"}}
        spec["measures"] = {
            "shared": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "left_branch": {"op": "fn", "fn": "counting_add", "inputs": ["shared", "shared"]},
            "right_branch": {"op": "fn", "fn": "counting_add", "inputs": ["shared", "shared"]},
            "top": {
                "op": "fn",
                "fn": "counting_add",
                "inputs": ["left_branch", "right_branch"],
            },
        }
        write_yaml(extra_config / "kpis" / "9908.yaml", spec)
        ctx = _context(path, extra_config, 9908, ["top"])
        row = find_row(
            compute(ctx, config_dir=extra_config),
            cut="R",
            reason="LATE_SUPPLIER",
            region="NA",
        )
        assert value_of(row, "top") == 40.0
        # One combo on cut R plus one on cut G: 3 fn calls each, not 7.
        assert len(calls) == 6
    finally:
        unregister_measure_fn("counting_add")


def test_fn_measure_requires_inputs_and_a_known_fn(extra_config, safe_ratio_fn):
    """op: fn needs both inputs: and a registered fn:."""
    missing_inputs = minimal_kpi(9909)
    missing_inputs["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "broken": {"op": "fn", "fn": safe_ratio_fn},
    }
    write_yaml(extra_config / "kpis" / "9909.yaml", missing_inputs)
    with pytest.raises(BindError, match="requires `inputs:`"):
        load_kpi(9909, extra_config)

    unknown_fn = minimal_kpi(9910)
    unknown_fn["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "broken": {"op": "fn", "fn": "nope", "inputs": ["current_value"]},
    }
    write_yaml(extra_config / "kpis" / "9910.yaml", unknown_fn)
    with pytest.raises(BindError, match="unknown fn 'nope'"):
        load_kpi(9910, extra_config)


def test_unknown_measure_reference_fails_at_bind(extra_config, safe_ratio_fn):
    """inputs: naming a measure that does not exist lists the valid keys."""
    spec = minimal_kpi(9911)
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "broken": {"op": "fn", "fn": safe_ratio_fn, "inputs": ["current_value", "ghost"]},
    }
    write_yaml(extra_config / "kpis" / "9911.yaml", spec)
    with pytest.raises(BindError, match="references unknown measure 'ghost'"):
        load_kpi(9911, extra_config)


def test_dependency_cycle_fails_at_bind(extra_config, safe_ratio_fn):
    """A -> B -> A is reported by name instead of recursing until RecursionError."""
    spec = minimal_kpi(9912)
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "a_measure": {"op": "fn", "fn": safe_ratio_fn, "inputs": ["b_measure", "current_value"]},
        "b_measure": {"op": "fn", "fn": safe_ratio_fn, "inputs": ["a_measure", "current_value"]},
    }
    write_yaml(extra_config / "kpis" / "9912.yaml", spec)
    with pytest.raises(BindError, match="dependency cycle"):
        load_kpi(9912, extra_config)


def test_self_referencing_measure_fails_at_bind(extra_config):
    """A measure cannot list itself as an operand."""
    spec = minimal_kpi(9913)
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "loop": {"op": "arithmetic", "fn": "div", "left": "loop", "right": "current_value"},
    }
    write_yaml(extra_config / "kpis" / "9913.yaml", spec)
    with pytest.raises(BindError, match="dependency cycle: loop -> loop"):
        load_kpi(9913, extra_config)


def test_misplaced_fn_and_inputs_are_rejected(extra_config):
    """fn: on a point measure and inputs: on arithmetic are not silently dropped."""
    spec = minimal_kpi(9914)
    spec["measures"] = {
        "current_value": {
            "of": "sotif_value",
            "op": "point",
            "offset": {"months": 0},
            "fn": "mul",
        },
    }
    write_yaml(extra_config / "kpis" / "9914.yaml", spec)
    with pytest.raises(BindError, match="ignores `fn:`"):
        load_kpi(9914, extra_config)

    spec2 = minimal_kpi(9915)
    spec2["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "combo": {
            "op": "arithmetic",
            "fn": "add",
            "left": "current_value",
            "right": "current_value",
            "inputs": ["current_value"],
        },
    }
    write_yaml(extra_config / "kpis" / "9915.yaml", spec2)
    with pytest.raises(BindError, match="ignores `inputs:`"):
        load_kpi(9915, extra_config)


def test_fn_measure_widens_the_scan_span(extra_config, safe_ratio_fn):
    """Lookback walks inputs, so a fn over a YoY point still scans 12 months."""
    from kpi_engine.pipeline.time_planner import lookback_for

    spec = minimal_kpi(9916)
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "previous_year_value": {"of": "sotif_value", "op": "point", "offset": {"years": 1}},
        "ratio": {
            "op": "fn",
            "fn": safe_ratio_fn,
            "inputs": ["current_value", "previous_year_value"],
        },
    }
    write_yaml(extra_config / "kpis" / "9916.yaml", spec)
    kpi = load_kpi(9916, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert lookback_for(by_key["ratio"], by_key, kpi.time) == 12


def test_median_of_a_column_op_uses_the_row_level_detail(tmp_path, extra_config):
    """agg: median on a column op is computed in Pandas from the retrieved rows."""
    path = _two_column_parquet(tmp_path, [_row(ontime=1, fullqty=10), _row(ontime=2, fullqty=4)])
    spec = minimal_kpi(
        9917,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "sotif_value": {"columns": ["ontime", "fullqty"], "op": "multiply", "agg": "median"}
    }
    write_yaml(extra_config / "kpis" / "9917.yaml", spec)
    assert _only_value(path, extra_config, 9917) == 9.0  # median of 10 and 8


def test_named_inputs_bind_by_parameter_not_by_order(tmp_path, extra_config):
    """growth_pct(current, previous) is fed by name, so YoY cannot come out inverted."""
    path = _two_column_parquet(
        tmp_path,
        [_row(month="2025-03-01", fullqty=10), _row(month="2026-03-01", fullqty=15)],
    )
    spec = minimal_kpi(9940)
    spec["base_measures"] = {"sotif_value": {"sql": "fullqty", "agg": "sum"}}
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "previous_year_value": {"of": "sotif_value", "op": "point", "offset": {"years": 1}},
        "yoy_growth": {
            "op": "fn",
            "fn": "growth_pct",
            "inputs": {"previous": "previous_year_value", "current": "current_value"},
        },
    }
    write_yaml(extra_config / "kpis" / "9940.yaml", spec)
    assert _only_value(path, extra_config, 9940, "yoy_growth") == pytest.approx(0.5)


def test_measure_fn_arity_is_checked_at_bind(extra_config):
    """Too few inputs for a three-parameter function is caught before any scan."""

    def blend(base, bonus, penalty):
        """Three-way blend used only to pin down arity checking."""
        return (base or 0) + (bonus or 0) - (penalty or 0)

    register_measure_fn("blend", blend)
    try:
        spec = minimal_kpi(9941)
        spec["measures"] = {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "blended": {
                "op": "fn",
                "fn": "blend",
                "inputs": ["current_value", "current_value"],
            },
        }
        write_yaml(extra_config / "kpis" / "9941.yaml", spec)
        with pytest.raises(BindError, match="needs at least 3 inputs"):
            load_kpi(9941, extra_config)
    finally:
        unregister_measure_fn("blend")


def test_binary_measure_fn_still_folds_a_longer_operand_list(tmp_path, extra_config):
    """arithmetic over three operands keeps folding left to right."""
    path = _two_column_parquet(tmp_path, [_row(fullqty=2)])
    spec = minimal_kpi(9942)
    spec["base_measures"] = {"sotif_value": {"sql": "fullqty", "agg": "sum"}}
    spec["measures"] = {
        "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
        "cubed": {
            "op": "arithmetic",
            "fn": "multiply",
            "of": ["current_value", "current_value", "current_value"],
        },
    }
    write_yaml(extra_config / "kpis" / "9942.yaml", spec)
    assert _only_value(path, extra_config, 9942, "cubed") == 8.0
