"""Gold pattern KPIs under kpi_config/patterns/ (Phase 1+)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, value_of

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ROOT / "kpi_config" / "patterns"


def _install_pattern(extra_config: Path, stem: str, kpi_id: int) -> None:
    text = (PATTERNS / f"{stem}.yaml").read_text(encoding="utf-8")
    dest = extra_config / "kpis" / f"{kpi_id}.yaml"
    dest.write_text(text, encoding="utf-8")


def test_mask_and_weighted_pattern_computes(parquet_path, extra_config):
    _install_pattern(extra_config, "mask_and_weighted", 90001)
    kpi = load_kpi(90001, extra_config)
    names = {b.name: b for b in kpi.base_measures}
    assert names["weighted"].agg == "weighted_avg"
    assert names["late_only"].where.kind == "or"
    ctx = make_context(
        parquet_path,
        measures=["current_value", "weighted_value", "masked_value"],
        supplier=["ABC"],
        kpi_id=90001,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    other = find_row(result, cut="G", reason="OTHER")
    assert value_of(late, "current_value") == 45.0
    assert value_of(late, "weighted_value") is not None
    assert value_of(late, "masked_value") == 45.0
    assert value_of(other, "masked_value") == 6.0


def test_cross_cut_pattern_computes(parquet_path, extra_config):
    _install_pattern(extra_config, "cross_cut", 90002)
    kpi = load_kpi(90002, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["g_value"].from_cut == "G"
    assert by_key["share_vs_g"].versus_cut == "G"
    ctx = make_context(
        parquet_path,
        measures=["current_value", "g_value", "share_vs_g"],
        supplier=["ABC"],
        kpi_id=90002,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    g_late = find_row(result, cut="G", reason="LATE_SUPPLIER")
    r_na = find_row(result, cut="R", reason="LATE_SUPPLIER", region="NA")
    assert value_of(r_na, "g_value") == value_of(g_late, "current_value")
    assert value_of(r_na, "share_vs_g") is not None


def test_core_ops_pattern_computes(parquet_path, extra_config):
    _install_pattern(extra_config, "core_ops", 90003)
    kpi = load_kpi(90003, extra_config)
    keys = {m.key for m in kpi.measures}
    assert "value_band_low" in keys and "value_band_high" in keys
    ctx = make_context(
        parquet_path,
        measures=["current_value", "expanding", "value_band_low", "value_band_high", "growth_3"],
        supplier=["ABC"],
        kpi_id=90003,
        month="2026-03",
    )
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert value_of(row, "expanding") is not None
    low = value_of(row, "value_band_low")
    high = value_of(row, "value_band_high")
    current = value_of(row, "current_value")
    assert low == pytest.approx(current * 0.8)
    assert high == pytest.approx(current * 1.2)

