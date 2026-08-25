"""Request-time selected_dimensions overlay and extras-only cuts.

What this file provides
    Envelope shapes, bind errors, graph identity (not equal numbers), densify
    cap, G/R matrix, two-model supplier join, grain leak, result not_in_grain,
    and a grep gate that grouping reads go through effective_group_by.

Where it is used
    pytest tests/test_selected_dimensions.py.

When to use
    Add a case when the request grain overlay or cut extras rules change.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.calc_engine import densify
from kpi_engine.pipeline.cuts import effective_group_by
from kpi_engine.exceptions import BindError, CatalogError, ContextError
from tests.conftest import make_context, minimal_kpi, sotif_cuts, write_yaml


def _graph(kpi):
    """Measure math identity: keys, kinds, ops, and base aggs — not values."""
    return (
        [(m.name, m.agg, m.sql, m.row_op) for m in kpi.base_measures],
        [
            (m.key, m.kind, m.of, m.fn, m.expr, m.offset, m.trailing_months)
            for m in kpi.measures
        ],
    )


def test_graph_identity_does_not_change_with_selected_dimensions(config_dir):
    """Same requested keys and kind/op/agg/of at default grain vs supplier."""
    default = load_kpi(3004, config_dir)
    supplier = load_kpi(3004, config_dir, selected_dimensions=("supplier",))
    assert _graph(default) == _graph(supplier)
    assert [c.group_by for c in default.cuts] == [c.group_by for c in supplier.cuts]
    assert default.request_grain == ("reason_code",)
    assert supplier.request_grain == ("supplier",)


def test_selected_dimensions_omit_empty_names_and_bool_object(
    parquet_path, extra_config
):
    """omit→defaults; []/{}→empty; names-object host order; bool-object catalog order."""
    spec = minimal_kpi(9060)
    write_yaml(extra_config / "kpis" / "9060.yaml", spec)
    base = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9060)

    omitted = compute(base, config_dir=extra_config)
    assert omitted["selected_dimensions"] == ["reason_code"]
    g = next(r for r in omitted["rows"] if r["output_cut"] == "G")
    assert g["grouped_dimensions"] == ["reason_code"]

    empty_list = compute({**base, "selected_dimensions": []}, config_dir=extra_config)
    assert empty_list["selected_dimensions"] == []
    g_empty = next(r for r in empty_list["rows"] if r["output_cut"] == "G")
    assert g_empty["grouped_dimensions"] == []
    r_empty = next(r for r in empty_list["rows"] if r["output_cut"] == "R")
    assert r_empty["grouped_dimensions"] == ["region"]

    empty_obj = compute({**base, "selected_dimensions": {}}, config_dir=extra_config)
    assert empty_obj["selected_dimensions"] == []

    named = compute(
        {**base, "selected_dimensions": {"names": ["supplier", "region"]}},
        config_dir=extra_config,
    )
    assert named["selected_dimensions"] == ["supplier", "region"]
    r_named = next(
        r
        for r in named["rows"]
        if r["output_cut"] == "R" and r.get("supplier") == "ABC"
    )
    assert r_named["grouped_dimensions"] == ["supplier", "region"]

    flags = compute(
        {**base, "selected_dimensions": {"supplier": True, "region": True}},
        config_dir=extra_config,
    )
    assert flags["selected_dimensions"] == ["region", "supplier"]


def test_unknown_false_and_empty_string_are_bind_errors(parquet_path, extra_config):
    """Unknown bool keys and blank names fail; they do not silently drop."""
    write_yaml(extra_config / "kpis" / "9061.yaml", minimal_kpi(9061))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9061)
    with pytest.raises(BindError, match="Unknown selected_dimensions"):
        compute({**ctx, "selected_dimensions": {"unknown": False}}, config_dir=extra_config)
    with pytest.raises(BindError, match="cannot be empty"):
        compute({**ctx, "selected_dimensions": [""]}, config_dir=extra_config)
    with pytest.raises(ContextError, match="array of names"):
        compute({**ctx, "selected_dimensions": 1}, config_dir=extra_config)


def test_parameters_selected_dimensions_is_forbidden(extra_config):
    """Grain overlay is context.selected_dimensions, never a YAML parameter."""
    spec = minimal_kpi(
        9062,
        parameters={"selected_dimensions": {"type": "list", "item": "string"}},
    )
    write_yaml(extra_config / "kpis" / "9062.yaml", spec)
    with pytest.raises(BindError, match="parameters.selected_dimensions is not allowed"):
        load_kpi(9062, extra_config)


def test_default_dimensions_required_and_extras_disjoint(extra_config):
    """Missing defaults, overlap with extras, and exclude∩extras are bind errors."""
    spec = minimal_kpi(9063)
    del spec["default_dimensions"]
    write_yaml(extra_config / "kpis" / "9063.yaml", spec)
    with pytest.raises(BindError, match="default_dimensions is required"):
        load_kpi(9063, extra_config)

    overlap = minimal_kpi(9064, cuts=sotif_cuts())
    overlap["cuts"][0]["group_by"] = ["reason_code"]
    write_yaml(extra_config / "kpis" / "9064.yaml", overlap)
    with pytest.raises(BindError, match="already in default_dimensions"):
        load_kpi(9064, extra_config)

    both = minimal_kpi(9065)
    both["cuts"][1]["exclude_from_grain"] = ["region"]
    write_yaml(extra_config / "kpis" / "9065.yaml", both)
    with pytest.raises(BindError, match="both group_by and exclude_from_grain"):
        load_kpi(9065, extra_config)


def test_ignore_exclude_coupling(extra_config):
    """Dim ignore tokens and exclude_from_grain must agree both ways."""
    spec = minimal_kpi(9066)
    spec["cuts"][0].pop("exclude_from_grain")
    write_yaml(extra_config / "kpis" / "9066.yaml", spec)
    with pytest.raises(BindError, match="add it to exclude_from_grain"):
        load_kpi(9066, extra_config)

    missing_ignore = minimal_kpi(9067)
    missing_ignore["cuts"][0]["ignore_filters"] = []
    write_yaml(extra_config / "kpis" / "9067.yaml", missing_ignore)
    with pytest.raises(BindError, match="requires ignore_filters"):
        load_kpi(9067, extra_config)


def test_grouped_dimensions_match_applied_cuts(parquet_path, extra_config):
    """Row grouped_dimensions is the same function as applied_cuts[].group_by."""
    write_yaml(extra_config / "kpis" / "9068.yaml", minimal_kpi(9068))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9068)
    result = compute(ctx, config_dir=extra_config)
    by_cut = {row["name"]: row["group_by"] for row in result["applied_cuts"]}
    for row in result["rows"]:
        assert row["grouped_dimensions"] == by_cut[row["output_cut"]]


def test_result_filter_not_in_grain_skips(parquet_path, extra_config):
    """apply: result on a dim outside this cut's effective grain does not hide G."""
    spec = minimal_kpi(
        9069,
        cuts=[
            {"name": "G", "group_by": [], "ignore_filters": [], "also_emit": ["R"]},
            {"name": "R", "group_by": ["region"], "ignore_filters": []},
        ],
    )
    spec["filters"] = {"region": {"column": "region", "op": "in", "apply": "result"}}
    write_yaml(extra_config / "kpis" / "9069.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=9069,
    )
    result = compute(ctx, config_dir=extra_config)
    assert any(r["output_cut"] == "G" for r in result["rows"])
    assert {"filter_code": "region", "reason": "not_in_grain"} in result["skipped_filters"]
    r_regions = {r["region"] for r in result["rows"] if r["output_cut"] == "R"}
    assert r_regions == {"NA"}


def test_default_cut_missing_is_bind_error(parquet_path, extra_config):
    """Default cut extras that are not on the extract fail bind, not dropped_cuts."""
    spec = minimal_kpi(
        9070,
        default_dimensions=[],
        cuts=[{"name": "G", "group_by": ["factor"], "ignore_filters": []}],
        dimensions=[
            {"name": "reason_code", "from": "reason_code"},
            {"name": "factor", "from": "factor"},
        ],
    )
    write_yaml(extra_config / "kpis" / "9070.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9070)
    with pytest.raises(BindError, match="Cut 'G' is not on this extract|cuts 'G' is not on this extract"):
        compute(ctx, config_dir=extra_config)


def test_also_emit_extra_missing_is_dropped_cuts(parquet_path, extra_config):
    """R extras that are absent from the extract drop; G still emits."""
    spec = minimal_kpi(
        9071,
        cuts=[
            {
                "name": "G",
                "group_by": [],
                "exclude_from_grain": ["region"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["factor"], "ignore_filters": []},
        ],
        dimensions=[
            {"name": "reason_code", "from": "reason_code"},
            {"name": "region", "from": "region"},
            {"name": "factor", "from": "factor"},
        ],
    )
    write_yaml(extra_config / "kpis" / "9071.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9071)
    result = compute(ctx, config_dir=extra_config)
    assert {r["output_cut"] for r in result["rows"]} == {"G"}
    assert any(d["name"] == "R" for d in result["dropped_cuts"])


def test_densify_cap_fails_before_building_the_grid():
    """n_combos × n_periods over 50_000 is CatalogError naming selected_dimensions."""
    frame = pd.DataFrame(
        {
            "k": range(50_001),
            "event_month": date(2026, 3, 1),
            "v": 1.0,
        }
    )
    with pytest.raises(CatalogError, match="selected_dimensions"):
        densify(
            frame,
            keys=["k"],
            time_col="event_month",
            start=date(2026, 3, 1),
            end=date(2026, 3, 1),
            value_cols=["v"],
            fill_zero_cols=[],
        )


def test_two_model_join_at_supplier(parquet_path, extra_config, tmp_path):
    """Join keys are time + supplier, not the unused default reason_code."""
    spend = tmp_path / "spend.parquet"
    pd.DataFrame(
        [
            {"event_month": "2026-03-01", "supplier_name": "ABC", "spend": 100},
        ]
    ).to_parquet(spend, index=False)
    write_yaml(
        extra_config / "models" / "marketing.yaml",
        {
            "model_id": "marketing",
            "kind": "physical",
            "required_aliases": ["marketing"],
            "sources": {"marketing": {"alias": "marketing"}},
            "joins": [],
        },
    )
    write_yaml(
        extra_config / "kpis" / "9072.yaml",
        {
            "kpi_id": 9072,
            "version": 1,
            "model": "sotif",
            "time": {
                "column": "event_month",
                "grain": "month",
                "filter_code": "reporting_month",
            },
            "dimensions": [
                {"name": "reason_code", "from": "reason_code"},
                {"name": "supplier", "from": "supplier_name"},
            ],
            "default_dimensions": ["reason_code"],
            "base_measures": {
                "sotif_value": {"sql": "amount", "agg": "sum", "model": "sotif"},
                "marketing_spend": {"sql": "spend", "agg": "sum", "model": "marketing"},
            },
            "model_relations": [
                {
                    "left": "sotif_value",
                    "right": "marketing_spend",
                    "on": ["event_month", "supplier"],
                    "how": "inner",
                }
            ],
            "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
            "default_cut": "G",
            "measures": {
                "current_sotif": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
                "current_spend": {
                    "of": "marketing_spend",
                    "op": "point",
                    "offset": {"months": 0},
                },
                "spend_ratio": {
                    "op": "arithmetic",
                    "fn": "div",
                    "left": "current_sotif",
                    "right": "current_spend",
                },
            },
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["current_sotif", "current_spend", "spend_ratio"],
        supplier=["ABC"],
        kpi_id=9072,
        selected_dimensions=["supplier"],
        extra_datasets={
            "Marketing": {
                "dataset_id": 40,
                "dataset_name": "MARKETING",
                "table_type": "PARQUET",
                "path": str(spend),
                "alias": "marketing",
                "columns": ["event_month", "supplier_name", "spend"],
                "filter_column_mappings": [],
            }
        },
    )
    result = compute(ctx, config_dir=extra_config)
    row = next(r for r in result["rows"] if r["output_cut"] == "G")
    assert row["grouped_dimensions"] == ["supplier"]
    assert row["supplier"] == "ABC"
    assert row["reason_code"] is None
    assert row["current_sotif"] == 51.0
    assert row["current_spend"] == 100.0
    assert row["spend_ratio"] == 0.51
    sql = " ".join(result["sqls"])
    assert "reason_code" not in sql.lower() or "supplier_name" in sql.lower()
    assert "supplier_name" in sql.lower()


def test_grain_does_not_leak_across_computes(parquet_path, extra_config):
    """Two computes in one process keep independent request_grain overlays."""
    write_yaml(extra_config / "kpis" / "9073.yaml", minimal_kpi(9073))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9073)
    first = compute(ctx, config_dir=extra_config)
    second = compute({**ctx, "selected_dimensions": ["supplier"]}, config_dir=extra_config)
    assert first["selected_dimensions"] == ["reason_code"]
    assert second["selected_dimensions"] == ["supplier"]
    assert all(r["grouped_dimensions"] == ["reason_code"] or r["grouped_dimensions"] == ["reason_code", "region"] for r in first["rows"])
    assert all("supplier" in r["grouped_dimensions"] or r["output_cut"] == "R" for r in second["rows"])
    third = compute(ctx, config_dir=extra_config)
    assert third["selected_dimensions"] == ["reason_code"]


def test_validate_sql_grain_matches_compute(parquet_path, extra_config):
    """validate and compute compile the same extract for a supplier grain."""
    write_yaml(extra_config / "kpis" / "9074.yaml", minimal_kpi(9074))
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9074,
        selected_dimensions=["supplier"],
    )
    planned = validate(ctx, config_dir=extra_config)
    computed = compute(ctx, config_dir=extra_config)
    assert planned["sql"] == computed["sql"]
    assert planned["selected_dimensions"] == ["supplier"]
    assert "supplier_name" in planned["sql"].lower()


def test_g_r_matrix_and_output_cut(parquet_path, extra_config):
    """Omit / [] / [supplier] × G+R, and output_cut lock."""
    spec = minimal_kpi(
        9075,
        parameters={"output_cut": {"type": "string", "default": "G", "allowed": ["G", "R"]}},
    )
    write_yaml(extra_config / "kpis" / "9075.yaml", spec)
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9075)
    omitted = compute(ctx, config_dir=extra_config)
    kpi = load_kpi(9075, extra_config)
    assert effective_group_by(kpi.cuts[0], kpi) == ("reason_code",)
    supplier_kpi = load_kpi(9075, extra_config, selected_dimensions=("supplier",))
    assert effective_group_by(supplier_kpi.cuts[0], supplier_kpi) == ("supplier",)
    assert effective_group_by(supplier_kpi.cuts[1], supplier_kpi) == ("supplier", "region")
    empty_kpi = load_kpi(9075, extra_config, selected_dimensions=())
    assert effective_group_by(empty_kpi.cuts[0], empty_kpi) == ()
    assert effective_group_by(empty_kpi.cuts[1], empty_kpi) == ("region",)
    locked = compute({**ctx, "parameters": {"output_cut": "R"}}, config_dir=extra_config)
    assert {r["output_cut"] for r in locked["rows"]} == {"R"}
    assert omitted["applied_cuts"][0]["group_by"] == ["reason_code"]


def test_cardinality_high_is_a_warning(parquet_path, extra_config):
    """cardinality: high does not fail bind; it appears on grain_warnings when selected."""
    spec = minimal_kpi(9076)
    spec["dimensions"][2]["cardinality"] = "high"
    write_yaml(extra_config / "kpis" / "9076.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=9076,
        selected_dimensions=["supplier"],
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["grain_warnings"] == [{"dimension": "supplier", "reason": "high_cardinality"}]


def test_grouping_reads_use_effective_group_by():
    """Leftover cut.group_by used as grouping keys outside parse/effective_group_by is a bug."""
    root = Path(__file__).resolve().parents[1] / "kpi_engine"
    allowed = {
        root / "pipeline" / "cuts.py",
        root / "pipeline" / "binder.py",
    }
    offenders = []
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "cut.group_by" in line or "c.group_by" in line:
                offenders.append(f"{path}:{i}:{line.strip()}")
    assert offenders == []
