"""Phase 1 host filters: required, default, inherit_filters / reset_filters."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, sotif_cuts, value_of, write_yaml


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_required_plus_default_is_bind_error(extra_config):
    _write(
        extra_config,
        94201,
        filters={
            "region": {
                "column": "region",
                "required": True,
                "default": ["NA"],
            }
        },
    )
    with pytest.raises(BindError, match="required: true and default"):
        load_kpi(94201, extra_config)


def test_required_omit_or_empty_is_bind_error(parquet_path, extra_config):
    _write(
        extra_config,
        94202,
        filters={"region": {"column": "region", "required": True}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=94202,
        month="2026-03",
    )
    with pytest.raises(BindError, match="required"):
        compute(ctx, config_dir=extra_config)

    ctx["filters"]["region"] = {"values": [], "input_text": "simple"}
    with pytest.raises(BindError, match="required"):
        compute(ctx, config_dir=extra_config)


def test_required_still_sent_when_cut_ignores(parquet_path, extra_config):
    """Required is request-level; G still worldwide for the ignored code."""
    _write(
        extra_config,
        94203,
        filters={"region": {"column": "region", "required": True}},
        measures={"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=94203,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    r = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    # G ignores region so NA+EU LATE = 30+15; R is NA only.
    assert value_of(g, "current_value") == pytest.approx(45.0)
    assert value_of(r, "current_value") == pytest.approx(30.0)


def test_default_applies_only_when_key_absent(parquet_path, extra_config):
    _write(
        extra_config,
        94204,
        filters={"region": {"column": "region", "default": ["NA"]}},
        measures={"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=94204,
        month="2026-03",
    )
    # absent → default NA
    result = compute(ctx, config_dir=extra_config)
    r = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(r, "current_value") == pytest.approx(30.0)

    ctx["filters"]["region"] = {"values": [], "input_text": "simple"}
    result = compute(ctx, config_dir=extra_config)
    # present [] skips default; both regions on R
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    assert value_of(na, "current_value") == pytest.approx(30.0)
    assert value_of(eu, "current_value") == pytest.approx(15.0)


def test_inherit_and_ignore_same_code_is_bind_error(extra_config):
    cuts = sotif_cuts()
    cuts[1]["inherit_filters"] = ["region"]
    cuts[1]["ignore_filters"] = ["region"]
    _write(extra_config, 94205, cuts=cuts)
    with pytest.raises(BindError, match="inherit_filters and ignore_filters"):
        load_kpi(94205, extra_config)


def test_reset_filters_skips_on_that_cut(parquet_path, extra_config):
    cuts = [
        {
            "name": "G",
            "group_by": [],
            "exclude_from_grain": ["region"],
            "ignore_filters": ["region"],
            "also_emit": ["R"],
        },
        {
            "name": "R",
            "group_by": ["region"],
            "reset_filters": ["Supplier Name"],
        },
    ]
    _write(
        extra_config,
        94206,
        cuts=cuts,
        measures={"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        kpi_id=94206,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    r = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(r, "current_value") == pytest.approx(30.0)


def test_inherit_filters_applies_at_calc_when_peer_ignores(parquet_path, extra_config):
    cuts = [
        {
            "name": "G",
            "group_by": [],
            "exclude_from_grain": ["region"],
            "ignore_filters": ["region"],
            "also_emit": ["R"],
        },
        {
            "name": "R",
            "group_by": ["region"],
            "inherit_filters": ["region"],
        },
    ]
    _write(
        extra_config,
        94207,
        cuts=cuts,
        measures={"current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}}},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        supplier=["ABC"],
        region=["NA"],
        kpi_id=94207,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    r = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(g, "current_value") == pytest.approx(45.0)
    assert value_of(r, "current_value") == pytest.approx(30.0)
