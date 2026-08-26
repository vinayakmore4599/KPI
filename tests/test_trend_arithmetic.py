"""op: trend_arithmetic — per-period ratio of totals and aligned trend zip.

What this file provides
    Bind guards, SP-style (a-b)/a series, previous-year offset, /0 null slots,
    series-zip hidden axes, and constant null coverage is in test_constant_and_rank.

Where it is used
    pytest tests/test_trend_arithmetic.py.

When to use
    Add a case when bind rules or per-period compose change.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from kpi_engine.pipeline.binder import load_kpi
from tests.conftest import find_row, make_context, minimal_kpi, value_of, write_yaml


def _po_frame() -> pd.DataFrame:
    rows: list[dict] = []
    facts = {
        date(2025, 1, 1): (200, 40),
        date(2025, 2, 1): (100, 0),
        date(2025, 3, 1): (80, 16),
        date(2026, 1, 1): (100, 20),
        date(2026, 2, 1): (50, 0),
        date(2026, 3, 1): (0, 0),
    }
    for month, (total, driven) in facts.items():
        rows.append(
            {
                "event_month": month,
                "region": "NA",
                "reason_code": "LATE_SUPPLIER",
                "supplier_name": "ABC",
                "amount": total,
                "total_po": total,
                "driven": driven,
            }
        )
    return pd.DataFrame(rows)


def _po_parquet(tmp_path: Path) -> Path:
    path = tmp_path / "po.parquet"
    _po_frame().to_parquet(path, index=False)
    return path


def _po_kpi(kpi_id: int, measures: dict, **overrides) -> dict:
    spec = minimal_kpi(
        kpi_id,
        base_measures={
            "total_po": {"sql": "total_po", "agg": "sum"},
            "driven": {"sql": "driven", "agg": "sum"},
        },
        measures=measures,
    )
    spec.update(overrides)
    if "measures" in overrides:
        spec["measures"] = overrides["measures"]
    if "base_measures" in overrides:
        spec["base_measures"] = overrides["base_measures"]
    return spec


def _po_context(parquet_path: Path, measures: list[str], kpi_id: int) -> dict:
    ctx = make_context(
        parquet_path,
        measures=measures,
        supplier=["ABC"],
        kpi_id=kpi_id,
    )
    cols = ctx["datasets"]["Sotif"]["columns"]
    for name in ("total_po", "driven"):
        if name not in cols:
            cols.append(name)
    return ctx


def _bind(extra_config: Path, kpi_id: int, spec: dict):
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return load_kpi(kpi_id, extra_config)


def test_trend_of_composite_still_fails(extra_config):
    spec = minimal_kpi(
        99100,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "current_period_trend": {
                "of": "current_value",
                "op": "trend",
                "trailing": {"months": 12},
            },
        },
    )
    with pytest.raises(BindError, match="not a base measure"):
        _bind(extra_config, 99100, spec)


def test_trend_arithmetic_rejects_mixed_base_and_trend(extra_config):
    spec = _po_kpi(
        99101,
        {
            "total_po_trend": {
                "of": "total_po",
                "op": "trend",
                "trailing": {"months": 3},
            },
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["total_po", "total_po_trend"],
                "trailing": {"months": 3},
            },
        },
    )
    with pytest.raises(BindError, match="cannot mix"):
        _bind(extra_config, 99101, spec)


def test_trend_arithmetic_rejects_scalar_dep(extra_config):
    spec = _po_kpi(
        99102,
        {
            "current_value": {"of": "total_po", "op": "point"},
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["current_value", "driven"],
                "trailing": {"months": 3},
            },
        },
    )
    with pytest.raises(BindError, match="cannot mix|not a trend"):
        _bind(extra_config, 99102, spec)


def test_trend_arithmetic_rejects_helper_and_non_additive(extra_config):
    spec = _po_kpi(
        99103,
        {
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["line", "total_po"],
                "trailing": {"months": 3},
            }
        },
        base_measures={
            "line": {"expr": "amount"},
            "total_po": {"sql": "total_po", "agg": "sum"},
        },
    )
    with pytest.raises(BindError, match="row helper"):
        _bind(extra_config, 99103, spec)

    spec2 = _po_kpi(
        99104,
        {
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["mid", "total_po"],
                "trailing": {"months": 3},
            }
        },
        base_measures={
            "mid": {"sql": "amount", "agg": "median"},
            "total_po": {"sql": "total_po", "agg": "sum"},
        },
    )
    with pytest.raises(BindError, match="non-additive"):
        _bind(extra_config, 99104, spec2)


def test_trend_arithmetic_rejects_fn_and_expr_and_date_fn(extra_config):
    spec = _po_kpi(
        99105,
        {
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "expr": "total_po / driven",
                "of": ["total_po", "driven"],
                "trailing": {"months": 3},
            }
        },
    )
    with pytest.raises(BindError, match="both"):
        _bind(extra_config, 99105, spec)

    spec2 = _po_kpi(
        99106,
        {
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "date_diff",
                "of": ["total_po", "driven"],
                "trailing": {"months": 3},
            }
        },
    )
    with pytest.raises(BindError, match="date function"):
        _bind(extra_config, 99106, spec2)

    spec3 = _po_kpi(
        99107,
        {
            "ratio": {
                "op": "trend_arithmetic",
                "of": ["total_po", "driven"],
                "trailing": {"months": 3},
            }
        },
    )
    with pytest.raises(BindError, match="requires `fn:` or `expr:`"):
        _bind(extra_config, 99107, spec3)


def test_series_parent_cannot_set_trailing(extra_config):
    spec = _po_kpi(
        99108,
        {
            "total_po_trend": {
                "of": "total_po",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "driven_trend": {
                "of": "driven",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["total_po_trend", "driven_trend"],
                "trailing": {"months": 3},
            },
        },
    )
    with pytest.raises(BindError, match="series mode cannot set"):
        _bind(extra_config, 99108, spec)


def test_series_mismatched_windows_and_cuts(extra_config):
    spec = _po_kpi(
        99109,
        {
            "a": {
                "of": "total_po",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "b": {
                "of": "driven",
                "op": "trend",
                "trailing": {"months": 12},
                "cuts": ["G"],
            },
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["a", "b"],
            },
        },
    )
    with pytest.raises(BindError, match="must share"):
        _bind(extra_config, 99109, spec)

    spec2 = _po_kpi(
        99110,
        {
            "a": {
                "of": "total_po",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "b": {
                "of": "driven",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G", "R"],
            },
            "ratio": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["a", "b"],
            },
        },
    )
    with pytest.raises(BindError, match="must share"):
        _bind(extra_config, 99110, spec2)


def test_base_expr_matches_sp_ratio_and_keeps_gap_slots(tmp_path, extra_config):
    parquet = _po_parquet(tmp_path)
    spec = _po_kpi(
        99111,
        {
            "sotif_pct_trend": {
                "op": "trend_arithmetic",
                "of": ["total_po", "driven"],
                "expr": "(total_po - driven) / total_po",
                "trailing": {"months": 3},
                "inclusive": True,
                "cuts": ["G"],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "99111.yaml", spec)
    ctx = _po_context(parquet, ["sotif_pct_trend"], 99111)
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    values = value_of(row, "sotif_pct_trend")
    assert len(values) == 3
    assert values[0] == pytest.approx(0.8)
    assert values[1] == pytest.approx(1.0)
    assert values[2] is None
    assert "sotif_pct_trend" in result["trend_axes"]
    assert len(result["trend_axes"]["sotif_pct_trend"]) == 3


def test_base_offset_years_shifts_axis(tmp_path, extra_config):
    parquet = _po_parquet(tmp_path)
    spec = _po_kpi(
        99112,
        {
            "sotif_pct_trend_ly": {
                "op": "trend_arithmetic",
                "of": ["total_po", "driven"],
                "expr": "(total_po - driven) / total_po",
                "trailing": {"months": 3},
                "offset": {"years": 1},
                "cuts": ["G"],
            }
        },
    )
    write_yaml(extra_config / "kpis" / "99112.yaml", spec)
    ctx = _po_context(parquet, ["sotif_pct_trend_ly"], 99112)
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    values = value_of(row, "sotif_pct_trend_ly")
    axis = result["trend_axes"]["sotif_pct_trend_ly"]
    assert axis[0].startswith("2025-01")
    assert axis[-1].startswith("2025-03")
    assert values == pytest.approx([0.8, 1.0, 0.8])


def test_series_zip_matches_expr_and_hides_dep_axes(tmp_path, extra_config):
    parquet = _po_parquet(tmp_path)
    spec = _po_kpi(
        99113,
        {
            "total_po_trend": {
                "of": "total_po",
                "op": "trend",
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "otif_numerator_trend": {
                "op": "trend_arithmetic",
                "fn": "subtract",
                "of": ["total_po", "driven"],
                "trailing": {"months": 3},
                "cuts": ["G"],
            },
            "sotif_pct_trend": {
                "op": "trend_arithmetic",
                "fn": "divide",
                "of": ["otif_numerator_trend", "total_po_trend"],
                "cuts": ["G"],
            },
        },
    )
    write_yaml(extra_config / "kpis" / "99113.yaml", spec)
    ctx = _po_context(parquet, ["sotif_pct_trend"], 99113)
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert "total_po_trend" not in row
    assert "otif_numerator_trend" not in row
    assert set(result["trend_axes"]) == {"sotif_pct_trend"}
    values = value_of(row, "sotif_pct_trend")
    assert values[0] == pytest.approx(0.8)
    assert values[1] == pytest.approx(1.0)
    assert values[2] is None
