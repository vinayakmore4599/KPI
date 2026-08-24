"""Request parameters: envelope, reserved overlays, expr inject, 3004 policy.

What this file provides
    context.parameters vs filters vs execution; time_grain and output_cut;
    scalar expr injection; unknown/missing/allowed bind errors.

Where it is used
    pytest tests/test_parameters.py.
"""

import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError, FilterError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def test_3004_rejects_undeclared_parameters(parquet_path, config_dir):
    ctx = make_context(
        parquet_path, measures=["current_value"], parameters={"Level": "G"}
    )
    with pytest.raises(BindError, match="declares no parameters"):
        validate(ctx, config_dir=config_dir)


def test_3004_accepts_omitted_or_empty_parameters(parquet_path, config_dir):
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir)
    assert result["request_parameters"] == {}
    ctx["parameters"] = {}
    assert compute(ctx, config_dir=config_dir)["request_parameters"] == {}


def test_parameter_default_and_allowed(parquet_path, extra_config):
    spec = minimal_kpi(
        9801,
        parameters={
            "threshold": {"type": "int", "default": 10, "allowed": [5, 10, 15]},
        },
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {
                "op": "expr",
                "expr": "if_else(current_value >= threshold, 1, 0)",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9801.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["flag"], supplier=["ABC"], kpi_id=9801
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["request_parameters"]["threshold"] == 10
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["flag"] == 1.0

    ctx["parameters"] = {"threshold": 7}
    with pytest.raises(BindError, match="not allowed"):
        validate(ctx, config_dir=extra_config)


def test_unknown_parameter_key_is_rejected(parquet_path, extra_config):
    spec = minimal_kpi(
        9802,
        parameters={"threshold": {"type": "int", "default": 0}},
    )
    write_yaml(extra_config / "kpis" / "9802.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9802,
        parameters={"threshold": 1, "extra": "nope"},
    )
    with pytest.raises(BindError, match="Unknown parameter"):
        validate(ctx, config_dir=extra_config)


def test_missing_required_parameter_is_rejected(parquet_path, extra_config):
    spec = minimal_kpi(
        9803,
        parameters={"Level": {"type": "string", "allowed": ["G", "Y", "R"]}},
    )
    write_yaml(extra_config / "kpis" / "9803.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9803)
    with pytest.raises(BindError, match="Missing required parameter"):
        validate(ctx, config_dir=extra_config)


def test_map_alias_then_allowed(parquet_path, extra_config):
    spec = minimal_kpi(
        9804,
        parameters={
            "Level": {
                "type": "string",
                "map": {"Green": "G", "Yellow": "Y", "Red": "R"},
                "allowed": ["G", "Y", "R"],
            }
        },
        measures={
            "g_amt": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "picked": {
                "op": "expr",
                "expr": "if_else(Level = 'G', g_amt, 0)",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9804.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["picked"],
        supplier=["ABC"],
        kpi_id=9804,
        parameters={"Level": "Green"},
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["request_parameters"]["Level"] == "G"
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert row["picked"] == 30.0


def test_output_cut_skips_also_emit(parquet_path, extra_config):
    spec = minimal_kpi(
        9805,
        parameters={"output_cut": {"type": "string", "default": "G", "allowed": ["G", "R"]}},
    )
    write_yaml(extra_config / "kpis" / "9805.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9805
    )
    result = compute(ctx, config_dir=extra_config)
    cuts = {row["output_cut"] for row in result["rows"]}
    assert cuts == {"G"}
    assert result["request_parameters"]["output_cut"] == "G"

    ctx["parameters"] = {"output_cut": "R"}
    result_r = compute(ctx, config_dir=extra_config)
    assert {row["output_cut"] for row in result_r["rows"]} == {"R"}


def test_time_grain_param_picks_week(parquet_path, extra_config):
    spec = minimal_kpi(
        9806,
        time={
            "column": "event_month",
            "grain": "month",
            "source_grain": "day",
            "grains": ["day", "week", "month"],
            "filter_code": "reporting_month",
        },
        parameters={"time_grain": {"type": "string"}},
        data_points={"day": 30, "week": 12, "month": 12},
    )
    write_yaml(extra_config / "kpis" / "9806.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9806,
        month="2026-03-23",
        time_grain="week",
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["time_grain"] == "week"
    assert planned["request_parameters"] == {"time_grain": "week"}


def test_execution_time_grain_leftover_errors(parquet_path, extra_config):
    spec = minimal_kpi(
        9807,
        parameters={"time_grain": {"type": "string", "default": "month"}},
    )
    write_yaml(extra_config / "kpis" / "9807.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9807)
    ctx["execution"]["time_grain"] = "week"
    with pytest.raises(BindError, match="execution.time_grain is not supported"):
        validate(ctx, config_dir=extra_config)


def test_interval_in_filters_hints_parameters(parquet_path, extra_config):
    spec = minimal_kpi(9808)
    write_yaml(extra_config / "kpis" / "9808.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9808,
        extra_filters={"Interval": {"value": ["week"], "input_text": "simple"}},
    )
    with pytest.raises(FilterError, match="context.parameters"):
        compute(ctx, config_dir=extra_config)


def test_parameter_name_collides_with_measure(extra_config):
    spec = minimal_kpi(
        9809,
        parameters={"current_value": {"type": "int", "default": 1}},
    )
    write_yaml(extra_config / "kpis" / "9809.yaml", spec)
    with pytest.raises(BindError, match="collide with measure keys"):
        from kpi_engine.core.binder import load_kpi

        load_kpi(9809, extra_config)
