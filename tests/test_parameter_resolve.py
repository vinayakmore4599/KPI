"""Parameter resolve v2: when:, from_param:, list/in, complete-pack checks."""

from __future__ import annotations

import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def _level_pack(kpi_id: int, **overrides) -> dict:
    spec = minimal_kpi(
        kpi_id,
        parameters={
            "Level": {
                "type": "string",
                "default": "G",
                "allowed": ["G", "Y", "R"],
            }
        },
        measures={
            "g_amt": {"op": "constant", "value": 1},
            "y_amt": {"op": "constant", "value": 2},
            "r_amt": {"op": "constant", "value": 3},
            "picked": {
                "when": {
                    "param": "Level",
                    "cases": {
                        "G": {"op": "expr", "expr": "g_amt"},
                        "Y": {"op": "expr", "expr": "y_amt"},
                    },
                    "else": {"op": "expr", "expr": "r_amt"},
                }
            },
        },
    )
    spec.update(overrides)
    return spec


def _write_physical_model(extra_config, model_id: str, **overrides) -> None:
    payload = {
        "model_id": model_id,
        "kind": "physical",
        "required_aliases": ["sotif"],
        "sources": {"sotif": {"alias": "sotif"}},
        "joins": [],
        "output_schema": [
            "event_month",
            "region",
            "reason_code",
            "supplier_name",
            "amount",
        ],
    }
    payload.update(overrides)
    write_yaml(extra_config / "models" / f"{model_id}.yaml", payload)


def test_when_picks_g_vs_else(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9810.yaml", _level_pack(9810))
    ctx = make_context(
        parquet_path, measures=["picked"], supplier=["ABC"], kpi_id=9810
    )
    row_g = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row_g, "picked") == 1.0
    ctx["parameters"] = {"Level": "R"}
    row_r = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row_r, "picked") == 3.0


def test_when_case_body_can_be_compare(parquet_path, extra_config):
    spec = minimal_kpi(
        9930,
        parameters={
            "Level": {
                "type": "string",
                "default": "G",
                "allowed": ["G", "Y"],
            }
        },
        measures={
            "picked": {
                "when": {
                    "param": "Level",
                    "cases": {
                        "G": {"op": "compare", "of": "sotif_value", "mode": "yoy"},
                    },
                    "else": {"of": "sotif_value", "op": "point"},
                }
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9930.yaml", spec)
    kpi = load_kpi(9930, extra_config)
    spec_g = {m.key: m for m in kpi.measures}["picked"]
    assert spec_g.kind == "pct_change"
    ctx = make_context(
        parquet_path, measures=["picked"], supplier=["ABC"], kpi_id=9930
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "picked") is not None
    kpi_y = load_kpi(9930, extra_config, parameters={"Level": "Y"})
    assert {m.key: m for m in kpi_y.measures}["picked"].kind == "point"


def test_when_missing_else_errors(extra_config):
    spec = _level_pack(9811)
    spec["measures"]["picked"]["when"].pop("else")
    write_yaml(extra_config / "kpis" / "9811.yaml", spec)
    with pytest.raises(BindError, match="missing required"):
        load_kpi(9811, extra_config)


def test_when_unknown_param_errors(extra_config):
    spec = _level_pack(9812)
    spec["measures"]["picked"]["when"]["param"] = "Nope"
    write_yaml(extra_config / "kpis" / "9812.yaml", spec)
    with pytest.raises(BindError, match="not a declared parameter"):
        load_kpi(9812, extra_config)


def test_when_case_key_not_in_allowed_errors(extra_config):
    spec = _level_pack(9813)
    spec["measures"]["picked"]["when"]["cases"]["Z"] = {"op": "expr", "expr": "g_amt"}
    write_yaml(extra_config / "kpis" / "9813.yaml", spec)
    with pytest.raises(BindError, match="not in parameter"):
        load_kpi(9813, extra_config)


def test_when_match_after_map(parquet_path, extra_config):
    spec = _level_pack(9814)
    spec["parameters"]["Level"]["map"] = {"Green": "G", "Yellow": "Y"}
    write_yaml(extra_config / "kpis" / "9814.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["picked"],
        supplier=["ABC"],
        kpi_id=9814,
        parameters={"Level": "Yellow"},
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "picked") == 2.0


def test_when_str_match_yaml_int_case(parquet_path, extra_config):
    spec = minimal_kpi(
        9815,
        parameters={"n": {"type": "int", "default": 1, "allowed": [1, 2]}},
        measures={
            "picked": {
                "when": {
                    "param": "n",
                    "cases": {
                        1: {"op": "constant", "value": 10},
                        2: {"op": "constant", "value": 20},
                    },
                    "else": {"op": "constant", "value": 0},
                }
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9815.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["picked"], supplier=["ABC"], kpi_id=9815
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "picked") == 10.0


def test_from_param_trailing_months(extra_config):
    spec = minimal_kpi(
        9816,
        parameters={"lookback": {"type": "int", "default": 3}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "value_n": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": {"from_param": "lookback"}},
                "inclusive": True,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9816.yaml", spec)
    kpi = load_kpi(9816, extra_config, parameters={"lookback": 6})
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["value_n"].trailing_months == 6


def test_from_param_off_allowlist_errors(extra_config):
    spec = minimal_kpi(
        9817,
        parameters={"lookback": {"type": "int", "default": 3}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
                "inclusive": {"from_param": "lookback"},
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9817.yaml", spec)
    with pytest.raises(BindError, match="allowlist"):
        load_kpi(9817, extra_config)


def test_from_param_type_mismatch_errors(extra_config):
    spec = minimal_kpi(
        9818,
        parameters={"lookback": {"type": "string", "default": "3"}},
        measures={
            "value_n": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": {"from_param": "lookback"}},
                "inclusive": True,
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9818.yaml", spec)
    with pytest.raises(BindError, match="must be type int"):
        load_kpi(9818, extra_config)


def test_trailing_from_data_points_beside_from_param(extra_config):
    spec = minimal_kpi(
        9819,
        parameters={"lookback": {"type": "int", "default": 4}},
        data_points={"month": 12},
        measures={
            "from_points": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"from": "data_points"},
                "inclusive": True,
            },
            "from_param_n": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": {"from_param": "lookback"}},
                "inclusive": True,
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9819.yaml", spec)
    kpi = load_kpi(9819, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["from_points"].trailing_from == "data_points"
    assert by_key["from_param_n"].trailing_months == 4


def test_model_from_param_loads_other_file(parquet_path, extra_config):
    _write_physical_model(extra_config, "sotif_b")
    spec = minimal_kpi(
        9820,
        model={"from_param": "source"},
        parameters={
            "source": {
                "type": "string",
                "default": "sotif",
                "allowed": ["sotif", "sotif_b"],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9820.yaml", spec)
    kpi = load_kpi(9820, extra_config, parameters={"source": "sotif_b"})
    assert kpi.model_id == "sotif_b"
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9820,
        parameters={"source": "sotif_b"},
    )
    result = compute(ctx, config_dir=extra_config)
    assert value_of(find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA"), "current_value") == 30.0


def test_missing_column_on_chosen_model_errors_at_bind(extra_config):
    _write_physical_model(
        extra_config,
        "sotif_narrow",
        output_schema=["event_month", "region", "reason_code"],
    )
    spec = minimal_kpi(
        9821,
        model={"from_param": "source"},
        parameters={
            "source": {
                "type": "string",
                "default": "sotif",
                "allowed": ["sotif", "sotif_narrow"],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9821.yaml", spec)
    with pytest.raises(BindError, match="missing column 'amount'"):
        load_kpi(9821, extra_config, parameters={"source": "sotif_narrow"})


def test_templated_model_plus_base_model_errors(extra_config):
    spec = minimal_kpi(
        9822,
        model={"from_param": "source"},
        parameters={"source": {"type": "string", "default": "sotif"}},
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum", "model": "sotif"},
        },
    )
    write_yaml(extra_config / "kpis" / "9822.yaml", spec)
    with pytest.raises(BindError, match="base_measures"):
        load_kpi(9822, extra_config)


def test_list_param_in_expr(parquet_path, extra_config):
    spec = minimal_kpi(
        9823,
        parameters={
            "Level": {"type": "string", "default": "G"},
            "codes": {
                "type": "list",
                "item": "string",
                "default": ["G", "Y"],
            },
        },
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {
                "op": "expr",
                "expr": "if_else(Level in codes, current_value, 0)",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9823.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["flag"], supplier=["ABC"], kpi_id=9823
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(row, "flag") == 30.0
    ctx["parameters"] = {"Level": "R", "codes": ["G", "Y"]}
    row_miss = find_row(
        compute(ctx, config_dir=extra_config), cut="R", reason="LATE_SUPPLIER", region="NA"
    )
    assert value_of(row_miss, "flag") == 0.0


def test_list_literal_in(parquet_path, extra_config):
    spec = minimal_kpi(
        9824,
        parameters={"Level": {"type": "string", "default": "G"}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {
                "op": "expr",
                "expr": "if_else(Level in ('G', 'Y'), current_value, 0)",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9824.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["flag"], supplier=["ABC"], kpi_id=9824
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(row, "flag") == 30.0


def test_empty_list_in_is_false(parquet_path, extra_config):
    spec = minimal_kpi(
        9825,
        parameters={
            "Level": {"type": "string", "default": "G"},
            "codes": {"type": "list", "item": "string", "default": []},
        },
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {
                "op": "expr",
                "expr": "if_else(Level in codes, current_value, 0)",
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9825.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["flag"], supplier=["ABC"], kpi_id=9825
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(row, "flag") == 0.0


def test_adapter_list_on_scalar_param_errors_at_bind(parquet_path, extra_config):
    spec = minimal_kpi(
        9826,
        parameters={"Level": {"type": "string", "default": "G"}},
    )
    write_yaml(extra_config / "kpis" / "9826.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9826,
        parameters={"Level": ["G"]},
    )
    with pytest.raises(BindError, match="must be a string"):
        validate(ctx, config_dir=extra_config)


def test_load_kpi_defaults_and_required_when_param(extra_config):
    write_yaml(extra_config / "kpis" / "9827.yaml", _level_pack(9827))
    kpi = load_kpi(9827, extra_config, parameters={})
    assert kpi.bound_parameters["Level"] == "G"
    spec = _level_pack(9828)
    spec["parameters"]["Level"].pop("default")
    write_yaml(extra_config / "kpis" / "9828.yaml", spec)
    with pytest.raises(BindError, match="Missing required parameter"):
        load_kpi(9828, extra_config)


def test_compute_level_y_is_not_default_g_branch(parquet_path, extra_config):
    write_yaml(extra_config / "kpis" / "9829.yaml", _level_pack(9829))
    load_kpi(9829, extra_config)
    ctx = make_context(
        parquet_path,
        measures=["picked"],
        supplier=["ABC"],
        kpi_id=9829,
        parameters={"Level": "Y"},
    )
    row = find_row(compute(ctx, config_dir=extra_config), cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "picked") == 2.0


def test_unknown_measure_in_other_case_fails_at_bind(extra_config):
    spec = _level_pack(9831)
    spec["measures"]["picked"]["when"]["cases"]["Y"] = {
        "op": "expr",
        "expr": "does_not_exist",
    }
    write_yaml(extra_config / "kpis" / "9831.yaml", spec)
    with pytest.raises(BindError, match="does_not_exist"):
        load_kpi(9831, extra_config, parameters={"Level": "G"})


def test_base_measures_when_switches_sql(extra_config):
    spec = minimal_kpi(
        9832,
        parameters={"Level": {"type": "string", "default": "G", "allowed": ["G", "Y"]}},
        base_measures={
            "sotif_value": {
                "when": {
                    "param": "Level",
                    "cases": {
                        "G": {"sql": "amount", "agg": "sum"},
                        "Y": {"sql": "amount", "agg": "sum"},
                    },
                    "else": {"sql": "amount", "agg": "sum"},
                }
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9832.yaml", spec)
    kpi = load_kpi(9832, extra_config, parameters={"Level": "Y"})
    assert kpi.base_measures[0].sql == "amount"


def test_dataset_alias_hint_when_model_templated(parquet_path, extra_config):
    _write_physical_model(
        extra_config,
        "other_facts",
        required_aliases=["other_facts"],
        sources={"other_facts": {"alias": "other_facts"}},
    )
    spec = minimal_kpi(
        9833,
        model={"from_param": "source"},
        parameters={
            "source": {
                "type": "string",
                "default": "sotif",
                "allowed": ["sotif", "other_facts"],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9833.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=9833,
        parameters={"source": "other_facts"},
    )
    with pytest.raises(BindError, match="requires alias"):
        validate(ctx, config_dir=extra_config)


def test_when_on_cut_errors(extra_config):
    spec = _level_pack(9834)
    spec["cuts"][0] = {
        "name": "G",
        "when": {
            "param": "Level",
            "cases": {"G": {"group_by": ["reason_code"]}},
            "else": {"group_by": ["reason_code"]},
        },
    }
    write_yaml(extra_config / "kpis" / "9834.yaml", spec)
    with pytest.raises(BindError, match="when: is not allowed"):
        load_kpi(9834, extra_config)


def test_nested_when_errors(extra_config):
    spec = _level_pack(9835)
    spec["measures"]["picked"]["when"]["cases"]["G"] = {
        "when": {
            "param": "Level",
            "cases": {"G": {"op": "constant", "value": 1}},
            "else": {"op": "constant", "value": 0},
        }
    }
    write_yaml(extra_config / "kpis" / "9835.yaml", spec)
    with pytest.raises(BindError, match="when: is not allowed"):
        load_kpi(9835, extra_config)


def test_measure_when_and_from_param_together_errors(extra_config):
    spec = _level_pack(9836)
    spec["parameters"]["lookback"] = {"type": "int", "default": 3}
    spec["measures"]["picked"]["from_param"] = "lookback"
    write_yaml(extra_config / "kpis" / "9836.yaml", spec)
    with pytest.raises(BindError, match="mixes when:"):
        load_kpi(9836, extra_config)


def test_bound_value_else_or_param_errors(extra_config):
    spec = _level_pack(9837)
    spec["parameters"]["Level"]["map"] = {"X": "else"}
    spec["parameters"]["Level"]["allowed"] = ["G", "Y", "R", "else"]
    write_yaml(extra_config / "kpis" / "9837.yaml", spec)
    with pytest.raises(BindError, match="reserved as a when"):
        load_kpi(9837, extra_config, parameters={"Level": "X"})


def test_in_with_string_param_errors(extra_config):
    spec = minimal_kpi(
        9838,
        parameters={"Level": {"type": "string", "default": "G"}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {"op": "expr", "expr": "current_value in Level"},
        },
    )
    write_yaml(extra_config / "kpis" / "9838.yaml", spec)
    with pytest.raises(BindError, match="right side must be a list"):
        load_kpi(9838, extra_config)


def test_dict_ident_in_plus_errors(extra_config):
    spec = minimal_kpi(
        9839,
        parameters={"flags": {"type": "dict", "item": "int", "default": {"a": 1}}},
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "flag": {"op": "expr", "expr": "current_value + flags"},
        },
    )
    write_yaml(extra_config / "kpis" / "9839.yaml", spec)
    with pytest.raises(BindError, match="dict parameter"):
        load_kpi(9839, extra_config)


def test_dict_not_applied_as_fn_extras(parquet_path, extra_config):
    spec = minimal_kpi(
        9842,
        parameters={
            "flags": {
                "type": "dict",
                "item": "string",
                "default": {"numerator": "nope"},
            }
        },
        measures={
            "current_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"months": 0},
            },
            "ratio": {
                "op": "fn",
                "fn": "divide",
                "inputs": ["current_value", "current_value"],
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9842.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["ratio"], supplier=["ABC"], kpi_id=9842
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["request_parameters"]["flags"] == {"numerator": "nope"}
    row = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(row, "ratio") == 1.0


def test_hook_value_from_param_rejected(extra_config):
    spec = minimal_kpi(
        9843,
        parameters={"n": {"type": "int", "default": 1}},
        measures={
            "bar": {
                "op": "hook",
                "hook": "unused",
                "value": {"from_param": "n"},
            }
        },
    )
    write_yaml(extra_config / "kpis" / "9843.yaml", spec)
    with pytest.raises(BindError, match="allowlist"):
        load_kpi(9843, extra_config)


def test_double_equals_still_illegal_equals_works_in_when_expr(parquet_path, extra_config):
    spec = _level_pack(9844)
    spec["measures"]["picked"]["when"]["cases"]["G"] = {
        "op": "expr",
        "expr": "if_else(g_amt = 1, g_amt, 0)",
    }
    write_yaml(extra_config / "kpis" / "9844.yaml", spec)
    kpi = load_kpi(9844, extra_config)
    assert kpi.bound_parameters["Level"] == "G"
    spec["measures"]["picked"]["when"]["cases"]["G"] = {
        "op": "expr",
        "expr": "if_else(g_amt == 1, g_amt, 0)",
    }
    write_yaml(extra_config / "kpis" / "9845.yaml", spec)
    with pytest.raises(BindError, match="Illegal"):
        load_kpi(9845, extra_config)
