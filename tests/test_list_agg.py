"""Phase 3 list_agg / string_agg: point echo only, empty → null, overflow CatalogError."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml, value_of


def test_list_agg_point_returns_values(parquet_path, extra_config):
    spec = minimal_kpi(
        98001,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "amounts": {"sql": "amount", "agg": "list_agg"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "amount_list": {"of": "amounts", "op": "point"},
        },
    )
    write_yaml(extra_config / "kpis" / "98001.yaml", spec)
    kpi = load_kpi(98001, extra_config)
    assert {b.name: b.agg for b in kpi.base_measures}["amounts"] == "list_agg"
    ctx = make_context(
        parquet_path, measures=["amount_list"], supplier=["ABC"], kpi_id=98001
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cell = value_of(row, "amount_list")
    assert isinstance(cell, list)
    assert cell


def test_string_agg_point_joins(parquet_path, extra_config):
    spec = minimal_kpi(
        98002,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "codes": {"sql": "reason_code", "agg": "string_agg"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "code_join": {"of": "codes", "op": "point"},
        },
    )
    write_yaml(extra_config / "kpis" / "98002.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["code_join"], supplier=["ABC"], kpi_id=98002
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    cell = value_of(row, "code_join")
    assert isinstance(cell, str)
    assert "LATE_SUPPLIER" in cell


def test_list_agg_illegal_as_window_of(extra_config):
    spec = minimal_kpi(
        98003,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "amounts": {"sql": "amount", "agg": "list_agg"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "bad": {"of": "amounts", "op": "window", "trailing": {"months": 3}},
        },
    )
    write_yaml(extra_config / "kpis" / "98003.yaml", spec)
    with pytest.raises(BindError, match="list_agg/string_agg"):
        load_kpi(98003, extra_config)


def test_list_agg_overflow_is_catalog_error(parquet_path, extra_config, monkeypatch):
    from kpi_engine.capabilities.ops import support

    monkeypatch.setattr(support, "LIST_AGG_MAX_ITEMS", 1)
    spec = minimal_kpi(
        98004,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "amounts": {"sql": "amount", "agg": "list_agg"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "amount_list": {"of": "amounts", "op": "point"},
        },
    )
    write_yaml(extra_config / "kpis" / "98004.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["amount_list"], supplier=["ABC"], kpi_id=98004
    )
    with pytest.raises(CatalogError, match="overflow"):
        compute(ctx, config_dir=extra_config)
