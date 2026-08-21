"""Two-model tests: join aggregated extracts via model_relations.

What this file provides
    Outer/inner join of Sotif sums and marketing spend after each model's
    GROUP BY. Unmatched keys stay (outer) or drop (inner). Filters that exist
    only on one model are not applied to the other extract.

Where it is used
    pytest tests/test_model_relations.py.

When to use
    Add a case when a KPI authors a new model_relations join.
"""

import pandas as pd

from kpi_engine import compute
from kpi_engine.exceptions import BindError
from tests.conftest import make_context, write_yaml


def test_outer_join_keeps_sparse_side(parquet_path, extra_config, tmp_path):
    """Outer join: region with Sotif but no spend still appears; ratio is null."""
    spend = tmp_path / "spend.parquet"
    pd.DataFrame(
        [
            {"event_month": "2026-03-01", "region": "NA", "spend": 100},
            {"event_month": "2026-02-01", "region": "NA", "spend": 80},
            {"event_month": "2026-02-01", "region": "EU", "spend": 40},
            {"event_month": "2026-01-01", "region": "NA", "spend": 60},
            {"event_month": "2026-01-01", "region": "EU", "spend": 30},
        ]
    ).to_parquet(spend, index=False)
    _write_ratio_kpi(extra_config, how="outer")
    ctx = _ratio_context(parquet_path, spend)
    result = compute(ctx, config_dir=extra_config)
    na = next(r for r in result["rows"] if r["region"] == "NA")
    eu = next(r for r in result["rows"] if r["region"] == "EU")
    # NA March: LATE 30 + OTHER 6 = 36; spend 100
    assert na["current_sotif"] == 36.0
    assert na["current_spend"] == 100.0
    assert na["spend_ratio"] == 0.36
    # EU March: LATE 15; no spend row → outer keeps EU, ratio null
    assert eu["current_sotif"] == 15.0
    # No marketing row: outer keeps EU. Sum fill-zero on the spine may be 0; /0 → null.
    assert eu["current_spend"] in (None, 0.0)
    assert eu["spend_ratio"] is None
    assert "sqls" in result and len(result["sqls"]) == 2


def test_inner_join_drops_unmatched(parquet_path, extra_config, tmp_path):
    """Inner join drops the region that has Sotif but no marketing row."""
    spend = tmp_path / "spend.parquet"
    pd.DataFrame(
        [{"event_month": "2026-03-01", "region": "NA", "spend": 100}]
    ).to_parquet(spend, index=False)
    _write_ratio_kpi(extra_config, how="inner")
    ctx = _ratio_context(parquet_path, spend)
    result = compute(ctx, config_dir=extra_config)
    regions = {r["region"] for r in result["rows"]}
    assert regions == {"NA"}


def test_two_models_require_relations(parquet_path, extra_config, tmp_path):
    """Two base models without model_relations fail at bind."""
    write_yaml(extra_config / "models" / "marketing.yaml", _marketing_model())
    spec = _ratio_kpi("outer")
    spec.pop("model_relations")
    write_yaml(extra_config / "kpis" / "9030.yaml", spec)
    spend = tmp_path / "spend.parquet"
    pd.DataFrame([{"event_month": "2026-03-01", "region": "NA", "spend": 1}]).to_parquet(
        spend, index=False
    )
    try:
        compute(_ratio_context(parquet_path, spend), config_dir=extra_config)
    except BindError as exc:
        assert "model_relations" in str(exc)
    else:
        raise AssertionError("expected BindError")


def _write_ratio_kpi(extra_config, *, how: str) -> None:
    """KPI 9030 + marketing model YAML."""
    write_yaml(extra_config / "models" / "marketing.yaml", _marketing_model())
    write_yaml(extra_config / "kpis" / "9030.yaml", _ratio_kpi(how))


def _marketing_model() -> dict:
    """Physical model for the spend parquet."""
    return {
        "model_id": "marketing",
        "kind": "physical",
        "required_aliases": ["marketing"],
        "sources": {"marketing": {"alias": "marketing"}},
        "joins": [],
    }


def _ratio_kpi(how: str) -> dict:
    """Sotif / spend after aggregating each model separately."""
    return {
        "kpi_id": 9030,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
        },
        "dimensions": [{"name": "region", "kind": "dimension"}],
        "base_measures": {
            "sotif_value": {"sql": "amount", "agg": "sum", "model": "sotif"},
            "marketing_spend": {"sql": "spend", "agg": "sum", "model": "marketing"},
        },
        "model_relations": [
            {
                "left": "sotif_value",
                "right": "marketing_spend",
                "on": ["event_month", "region"],
                "how": how,
            }
        ],
        "cuts": [{"name": "R", "group_by": ["region"], "ignore_filters": []}],
        "default_cut": "R",
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
    }


def _ratio_context(parquet_path, spend_path) -> dict:
    """Context with Sotif facts plus a marketing spend dataset."""
    return make_context(
        parquet_path,
        measures=["current_sotif", "current_spend", "spend_ratio"],
        supplier=["ABC"],
        kpi_id=9030,
        extra_datasets={
            "Marketing": {
                "dataset_id": 40,
                "dataset_name": "MARKETING",
                "table_type": "PARQUET",
                "path": str(spend_path),
                "alias": "marketing",
                "columns": ["event_month", "region", "spend"],
                "filter_column_mappings": [],
            }
        },
    )
