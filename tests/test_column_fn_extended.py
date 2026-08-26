"""Phase 2 column functions: bind, lookback (0), and compute."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.time_planner import max_lookback_months
from tests.conftest import make_context, minimal_kpi, write_yaml, value_of


COLUMN_CASES = [
    ("safe_divide", {"columns": ["amount", "amount"], "op": "safe_divide", "agg": "sum"}),
    ("weighted_product", {"columns": ["amount", "amount"], "op": "weighted_product", "agg": "sum"}),
    ("trim", {"expr": "trim(region)", "agg": "count"}),
    ("upper", {"expr": "upper(region)", "agg": "count"}),
    ("lower", {"expr": "lower(region)", "agg": "count"}),
    ("concat", {"expr": "concat(region, reason_code)", "agg": "count"}),
    ("flag_in_set", {"expr": "flag_in_set(region, 'NA', 'EU')", "agg": "sum"}),
    ("parse_number", {"columns": ["amount"], "op": "parse_number", "agg": "sum"}),
    ("hash_bucket", {"expr": "hash_bucket(region, 4)", "agg": "max"}),
    ("substring", {"expr": "substring(reason_code, 0, 4)", "agg": "count"}),
    ("left", {"expr": "left(reason_code, 4)", "agg": "count"}),
    ("right", {"expr": "right(reason_code, 2)", "agg": "count"}),
    ("replace", {"expr": "replace(region, 'NA', 'US')", "agg": "count"}),
    ("clip", {"columns": ["amount", "amount", "amount"], "op": "clip", "agg": "sum"}),
    ("parse_date", {"columns": ["event_month"], "op": "parse_date", "agg": "max"}),
    ("coalesce_date", {"columns": ["event_month", "event_month"], "op": "coalesce_date", "agg": "max"}),
    (
        "is_between_dates",
        {"columns": ["event_month", "event_month", "event_month"], "op": "is_between_dates", "agg": "sum"},
    ),
]


@pytest.mark.parametrize("kpi_id,name,base", [
    (97400 + i, name, base) for i, (name, base) in enumerate(COLUMN_CASES)
])
def test_column_fn_bind(extra_config, kpi_id, name, base):
    spec = minimal_kpi(
        kpi_id,
        base_measures={"fact": base},
        measures={"probe": {"of": "fact", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    kpi = load_kpi(kpi_id, extra_config)
    assert {b.name: b for b in kpi.base_measures}["fact"]
    del name


@pytest.mark.parametrize("kpi_id,name,base", [
    (97450 + i, name, base) for i, (name, base) in enumerate(COLUMN_CASES)
])
def test_column_fn_lookback(extra_config, kpi_id, name, base):
    spec = minimal_kpi(
        kpi_id,
        base_measures={"fact": base},
        measures={"probe": {"of": "fact", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    kpi = load_kpi(kpi_id, extra_config)
    assert max_lookback_months(kpi, ("probe",)) >= 0
    del name


@pytest.mark.parametrize("kpi_id,name,base", [
    (97500 + i, name, base) for i, (name, base) in enumerate(COLUMN_CASES)
])
def test_column_fn_compute(parquet_path, extra_config, kpi_id, name, base):
    spec = minimal_kpi(
        kpi_id,
        base_measures={"fact": base},
        measures={"probe": {"of": "fact", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    ctx = make_context(parquet_path, measures=["probe"], supplier=["ABC"], kpi_id=kpi_id)
    result = compute(ctx, config_dir=extra_config)
    assert result["rows"]
    del name


def test_clip_and_json_extract(tmp_path, extra_config):
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1)],
            "reason_code": ["LATE_SUPPLIER"],
            "region": ["NA"],
            "supplier_name": ["ABC"],
            "amount": [10.0],
            "lo": [0.0],
            "hi": [5.0],
            "payload": ['{"a": 2}'],
        }
    )
    path = tmp_path / "clip.json.parquet"
    frame.to_parquet(path, index=False)
    spec = minimal_kpi(
        97590,
        base_measures={
            "clipped": {"columns": ["amount", "lo", "hi"], "op": "clip", "agg": "sum"},
            "extracted": {"expr": "json_extract(payload, '$.a')", "agg": "sum"},
        },
        measures={
            "clipped": {"of": "clipped", "op": "point"},
            "extracted": {"of": "extracted", "op": "point"},
        },
    )
    write_yaml(extra_config / "kpis" / "97590.yaml", spec)
    ctx = make_context(path, measures=["clipped", "extracted"], kpi_id=97590)
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    result = compute(ctx, config_dir=extra_config)
    row = result["rows"][0]
    assert value_of(row, "clipped") == pytest.approx(5.0)
    assert value_of(row, "extracted") == pytest.approx(2.0)
