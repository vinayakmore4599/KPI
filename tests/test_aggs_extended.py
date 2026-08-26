"""Phase 1 aggregations: geomean, harmonic_mean, any/all, weighted_avg re-agg."""

from __future__ import annotations

import math

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, value_of, write_yaml


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_weighted_avg_requires_weight_column(extra_config):
    _write(
        extra_config,
        94101,
        base_measures={"wavg": {"sql": "amount", "agg": "weighted_avg"}},
        measures={"cur": {"of": "wavg", "op": "point"}},
    )
    with pytest.raises(BindError, match="weight_column"):
        load_kpi(94101, extra_config)


def test_weighted_avg_g_r_reagg(parquet_path, extra_config):
    """G weighted average matches combining R carry columns, not averaging region avgs."""
    _write(
        extra_config,
        94102,
        base_measures={
            "wavg": {
                "sql": "amount",
                "agg": "weighted_avg",
                "weight_column": "amount",
            }
        },
        measures={"cur": {"of": "wavg", "op": "point"}},
    )
    ctx = make_context(
        parquet_path,
        measures=["cur"],
        supplier=["ABC"],
        kpi_id=94102,
        month="2026-03",
        extra_filters={"region": {"values": ["NA", "EU"], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    eu = find_row(result, cut="R", reason="LATE_SUPPLIER", region="EU")
    # Equal weights per row (amount is both value and weight) → mean of amounts.
    # NA Mar 2026 LATE = 10*3=30; EU = 5*3=15; G = (30*30 + 15*15) / (30+15) = 25.
    assert value_of(na, "cur") == pytest.approx(30.0)
    assert value_of(eu, "cur") == pytest.approx(15.0)
    assert value_of(g, "cur") == pytest.approx(25.0)


def test_geomean_and_harmonic_mean(parquet_path, extra_config):
    _write(
        extra_config,
        94103,
        base_measures={
            "geo": {"sql": "amount", "agg": "geomean"},
            "harm": {"sql": "amount", "agg": "harmonic_mean"},
        },
        measures={
            "geo_pt": {"of": "geo", "op": "point"},
            "harm_pt": {"of": "harm", "op": "point"},
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["geo_pt", "harm_pt"],
        supplier=["ABC"],
        kpi_id=94103,
        month="2026-03",
        extra_filters={"region": {"values": ["NA", "EU"], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=extra_config)
    g = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # NA 30, EU 15
    assert value_of(g, "geo_pt") == pytest.approx(math.sqrt(30 * 15))
    assert value_of(g, "harm_pt") == pytest.approx(2 / (1 / 30 + 1 / 15))


def test_any_all_empty_and_values(parquet_path, extra_config):
    _write(
        extra_config,
        94104,
        base_measures={
            "flag_any": {"sql": "amount", "agg": "any"},
            "flag_all": {"sql": "amount", "agg": "all"},
        },
        measures={
            "any_pt": {"of": "flag_any", "op": "point"},
            "all_pt": {"of": "flag_all", "op": "point"},
        },
    )
    ctx = make_context(
        parquet_path,
        measures=["any_pt", "all_pt"],
        supplier=["ABC"],
        kpi_id=94104,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(late, "any_pt") == pytest.approx(1.0)
    assert value_of(late, "all_pt") == pytest.approx(1.0)
    missing = find_row(result, cut="G", reason="OTHER")
    # OTHER has positive amounts too
    assert value_of(missing, "any_pt") == pytest.approx(1.0)
