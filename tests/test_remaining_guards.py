"""Guards that previously had no tests: physical joins, row_set, caps, filter_map.

What this file provides
    INNER/LEFT YAML joins, anchor_only vs span_union, TREND_CELL_CAP,
    output.page < 1, KPI YAML filter_map, illegal SQL identifiers.

Where it is used
    pytest tests/test_remaining_guards.py.

When to use
    Add a case when a binder/SQL/calc guard is added and is not covered elsewhere.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError, FilterError, KPIEngineError
from kpi_engine.identifiers import require_ident
from tests.conftest import find_row, make_context, sotif_cuts, write_yaml


def test_physical_inner_join_drops_unmatched_regions(parquet_path, extra_config, tmp_path):
    """kind: physical INNER JOIN keeps only fact rows that match the dim table."""
    regions_path = _write_na_only_regions(tmp_path)
    _write_join_model(extra_config, join_type="inner")
    write_yaml(extra_config / "kpis" / "9040.yaml", _kpi(9040, model="sotif_join"))
    ctx = _join_context(parquet_path, regions_path, kpi_id=9040)
    planned = validate(ctx, config_dir=extra_config)
    assert "INNER JOIN" in planned["sql"]
    assert '"sotif"."region"' in planned["sql"]

    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 30.0
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}


def test_physical_left_join_keeps_unmatched_regions(parquet_path, extra_config, tmp_path):
    """kind: physical LEFT JOIN keeps fact rows with no matching dim key."""
    regions_path = _write_na_only_regions(tmp_path)
    _write_join_model(extra_config, join_type="left")
    write_yaml(extra_config / "kpis" / "9041.yaml", _kpi(9041, model="sotif_join"))
    ctx = _join_context(parquet_path, regions_path, kpi_id=9041)
    planned = validate(ctx, config_dir=extra_config)
    assert "LEFT JOIN" in planned["sql"]

    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA", "EU"}


def test_unsupported_physical_join_type_fails(parquet_path, extra_config, tmp_path):
    """YAML join type must be LEFT, INNER, or RIGHT."""
    regions_path = _write_na_only_regions(tmp_path)
    _write_join_model(extra_config, join_type="cross")
    write_yaml(extra_config / "kpis" / "9042.yaml", _kpi(9042, model="sotif_join"))
    ctx = _join_context(parquet_path, regions_path, kpi_id=9042)
    with pytest.raises(BindError, match="Unsupported join type"):
        validate(ctx, config_dir=extra_config)


def test_anchor_only_drops_combos_missing_at_selected_month(tmp_path, extra_config):
    """row_set: anchor_only emits only dimension combos observed at the anchor."""
    path = _write_ghost_parquet(tmp_path)
    write_yaml(
        extra_config / "kpis" / "9043.yaml",
        _kpi(9043, row_set="anchor_only", also_emit=False),
    )
    ctx = make_context(
        path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9043,
    )
    result = compute(ctx, config_dir=extra_config)
    reasons = {row["reason_code"] for row in result["rows"] if row["output_cut"] == "G"}
    assert reasons == {"LIVE"}
    live = find_row(result, cut="G", reason="LIVE")
    assert live["current_value"] == 10.0
    assert live["value_3m"] == 30.0


def test_span_union_keeps_combos_with_only_lookback_activity(tmp_path, extra_config):
    """row_set: span_union keeps a combo that is active in the span but not at the anchor."""
    path = _write_ghost_parquet(tmp_path)
    write_yaml(
        extra_config / "kpis" / "9044.yaml",
        _kpi(9044, row_set="span_union", also_emit=False),
    )
    ctx = make_context(
        path,
        measures=["current_value", "value_3m"],
        supplier=["ABC"],
        kpi_id=9044,
    )
    result = compute(ctx, config_dir=extra_config)
    reasons = {row["reason_code"] for row in result["rows"] if row["output_cut"] == "G"}
    assert reasons == {"LIVE", "GHOST"}
    ghost = find_row(result, cut="G", reason="GHOST")
    assert ghost["current_value"] is None
    assert ghost["value_3m"] == 100.0


def test_invalid_row_set_fails_at_bind(extra_config):
    """row_set must be span_union or anchor_only."""
    write_yaml(extra_config / "kpis" / "9045.yaml", _kpi(9045, row_set="all_rows"))
    with pytest.raises(BindError, match="row_set"):
        load_kpi(9045, extra_config)


def test_trend_cell_cap_rejects_oversized_payload(parquet_path, config_dir, monkeypatch):
    """Trend length × cut rows cannot exceed TREND_CELL_CAP."""
    monkeypatch.setattr("kpi_engine.pipeline.calc_engine.TREND_CELL_CAP", 10)
    ctx = make_context(parquet_path, measures=["trend_12m"], supplier=["ABC"])
    with pytest.raises(KPIEngineError, match="exceeds 10"):
        compute(ctx, config_dir=config_dir)


def test_output_page_less_than_one_is_rejected(parquet_path, config_dir):
    """output.page must be >= 1 when page_size is set (0 is not treated as page 1)."""
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        page=0,
        page_size=2,
    )
    with pytest.raises(KPIEngineError, match="output.page must be >= 1"):
        compute(ctx, config_dir=config_dir)


def test_kpi_filter_map_binds_code_missing_from_context_mappings(parquet_path, extra_config):
    """KPI YAML filter_map maps a context filter_code when datasets have no mapping."""
    spec = _kpi(9046)
    write_yaml(extra_config / "kpis" / "9046.yaml", spec)
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        extra_filters={"plant_code": {"values": ["NA"], "input_text": "simple"}},
        kpi_id=9046,
    )
    with pytest.raises(FilterError, match="plant_code"):
        validate(ctx, config_dir=extra_config)

    spec["filter_map"] = {"plant_code": "region"}
    spec["cuts"][0]["ignore_filters"] = ["region", "plant_code"]
    write_yaml(extra_config / "kpis" / "9046.yaml", spec)
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert g["current_value"] == 45.0
    r_regions = {
        row["region"]
        for row in result["rows"]
        if row["output_cut"] == "R" and row["reason_code"] == "LATE_SUPPLIER"
    }
    assert r_regions == {"NA"}


def test_illegal_sql_identifiers_fail_at_bind(extra_config):
    """YAML names must be simple SQL identifiers; injection-shaped strings are rejected."""
    with pytest.raises(BindError, match="Illegal identifier"):
        require_ident("amount; DROP TABLE x", what="identifier")

    spec = _kpi(9047)
    spec["dimensions"] = [{"name": "reason-code", "kind": "dimension"}]
    write_yaml(extra_config / "kpis" / "9047.yaml", spec)
    with pytest.raises(BindError, match="Illegal dimension"):
        load_kpi(9047, extra_config)

    spec = _kpi(9048)
    spec["base_measures"]["sotif_value"]["sql"] = "amount; DROP TABLE x"
    write_yaml(extra_config / "kpis" / "9048.yaml", spec)
    with pytest.raises(BindError, match="Illegal measure sql"):
        load_kpi(9048, extra_config)

    write_yaml(
        extra_config / "models" / "bad_join.yaml",
        {
            "model_id": "bad_join",
            "kind": "physical",
            "required_aliases": ["sotif", "regions"],
            "sources": {"sotif": {"alias": "sotif"}, "regions": {"alias": "regions"}},
            "joins": [
                {
                    "left": "sotif",
                    "right": "regions",
                    "on": ["region;drop"],
                    "type": "inner",
                }
            ],
        },
    )
    from kpi_engine.pipeline.binder import load_model

    with pytest.raises(BindError, match="Illegal join.on"):
        load_model("bad_join", extra_config)


def _kpi(
    kpi_id: int,
    *,
    model: str = "sotif",
    row_set: str = "span_union",
    also_emit: bool = True,
) -> dict:
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": model,
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "default_dimensions": ["reason_code"],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": sotif_cuts(also_emit=also_emit),
        "default_cut": "G",
        "row_set": row_set,
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "value_3m": {
                "of": "sotif_value",
                "op": "window",
                "trailing": {"months": 3},
                "inclusive": True,
            },
        },
    }


def _write_join_model(extra_config, *, join_type: str) -> None:
    """Physical two-source model joined on region."""
    write_yaml(
        extra_config / "models" / "sotif_join.yaml",
        {
            "model_id": "sotif_join",
            "kind": "physical",
            "required_aliases": ["sotif", "regions"],
            "sources": {
                "sotif": {"alias": "sotif"},
                "regions": {"alias": "regions"},
            },
            "joins": [
                {
                    "left": "sotif",
                    "right": "regions",
                    "on": ["region"],
                    "type": join_type,
                }
            ],
        },
    )


def _write_na_only_regions(tmp_path) -> object:
    """Dim table that matches NA facts and omits EU."""
    path = tmp_path / "regions_na_only.parquet"
    pd.DataFrame([{"region": "NA", "eligible": True}]).to_parquet(path, index=False)
    return path


def _write_ghost_parquet(tmp_path) -> object:
    """LIVE exists at the anchor; GHOST exists only in the lookback month."""
    rows = [
        {
            "event_month": date(2026, 1, 1),
            "region": "NA",
            "reason_code": "LIVE",
            "supplier_name": "ABC",
            "amount": 10,
        },
        {
            "event_month": date(2026, 2, 1),
            "region": "NA",
            "reason_code": "LIVE",
            "supplier_name": "ABC",
            "amount": 10,
        },
        {
            "event_month": date(2026, 3, 1),
            "region": "NA",
            "reason_code": "LIVE",
            "supplier_name": "ABC",
            "amount": 10,
        },
        {
            "event_month": date(2026, 1, 1),
            "region": "NA",
            "reason_code": "GHOST",
            "supplier_name": "ABC",
            "amount": 100,
        },
    ]
    path = tmp_path / "ghost.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _join_context(parquet_path, regions_path, *, kpi_id: int) -> dict:
    """Context with facts + a region dim bound to the physical join model."""
    return make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=kpi_id,
        extra_datasets={
            "Regions": {
                "dataset_id": 22,
                "dataset_name": "REGIONS",
                "table_type": "PARQUET",
                "path": str(regions_path),
                "alias": "regions",
                "columns": ["region", "eligible"],
                "filter_column_mappings": [],
            }
        },
    )
