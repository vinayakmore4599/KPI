"""Isolated extract pipelines: one KPI, two models, no join unless a graph spans.

What this file provides
    load_kpi with two models and no relations; 157-only isolation (lookback,
    ignore_filters, cuts); both families in one request; incompatible cut;
    cross-model fn without relations.

Where it is used
    pytest tests/test_isolated_models.py.

When to use
    Add a case when a KPI authors a second extract without model_relations.
"""

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.core.binder import load_kpi
from kpi_engine.core.pipelines import partition_request
from kpi_engine.exceptions import BindError
from tests.conftest import make_context, write_yaml


def test_two_models_without_relations_load(extra_config):
    """Declaring two extracts does not require model_relations at load time."""
    _write_dual_kpi(extra_config)
    kpi = load_kpi(9040, extra_config)
    assert kpi.model_relations == ()
    assert {b.model_id or kpi.model_id for b in kpi.base_measures} == {"sotif", "reasons"}


def test_157_only_skips_156_lookback_and_ignore(parquet_path, extra_config, tmp_path):
    """A reasons-only request uses G157, the reasons span, and reasons ignore_filters."""
    reasons = _write_reasons(tmp_path)
    _write_dual_kpi(extra_config)
    ctx = _dual_context(
        parquet_path,
        reasons,
        measures=["reason_count", "percent_gt"],
        extra_filters={"reason_code": {"values": ["LATE_SUPPLIER"], "input_text": "simple"}},
    )
    planned = validate(ctx, config_dir=extra_config)
    assert planned["lookback_months"] == 0
    assert "reason_code" in planned["sql"]

    result = compute(ctx, config_dir=extra_config)
    cuts = {row["output_cut"] for row in result["rows"]}
    assert cuts == {"G157"}
    assert all(row["model"] == "reasons" for row in result["rows"])
    assert all(row.get("region") is None for row in result["rows"])
    factors = {row["factor"] for row in result["rows"]}
    assert factors == {"A"}
    assert "B" not in factors
    late = next(r for r in result["rows"] if r["reason_code"] == "LATE_SUPPLIER")
    assert late["reason_count"] == 10.0
    assert late["percent_gt"] == 100.0
    assert "reason_code" in result["sql"]


def test_both_families_keep_157_values_and_156_cuts(parquet_path, extra_config, tmp_path):
    """One request emits G156/R156 and G157; reasons numbers match a 157-only request."""
    reasons = _write_reasons(tmp_path)
    _write_dual_kpi(extra_config)
    only_157 = compute(
        _dual_context(parquet_path, reasons, measures=["reason_count", "percent_gt"]),
        config_dir=extra_config,
    )
    both = compute(
        _dual_context(
            parquet_path,
            reasons,
            measures=["current_value", "reason_count", "percent_gt"],
        ),
        config_dir=extra_config,
    )
    cuts = {row["output_cut"] for row in both["rows"]}
    assert {"G156", "R156", "G157"} <= cuts
    g157_only = {
        (r["reason_code"], r["factor"]): (r["reason_count"], r["percent_gt"])
        for r in only_157["rows"]
        if r["output_cut"] == "G157"
    }
    g157_both = {
        (r["reason_code"], r["factor"]): (r["reason_count"], r["percent_gt"])
        for r in both["rows"]
        if r["output_cut"] == "G157"
    }
    assert g157_both == g157_only
    sotif_rows = [r for r in both["rows"] if r["model"] == "sotif"]
    assert sotif_rows
    assert all(r["reason_count"] is None for r in sotif_rows)
    reasons_rows = [r for r in both["rows"] if r["model"] == "reasons"]
    assert all(r["current_value"] is None for r in reasons_rows)


def test_incompatible_cut_on_156_base_is_a_bind_error(parquet_path, extra_config, tmp_path):
    """cuts: [G157] on a sotif-only measure fails — factor is not on that extract."""
    reasons = _write_reasons(tmp_path)
    spec = _dual_kpi()
    spec["measures"]["current_value"]["cuts"] = ["G157"]
    write_yaml(extra_config / "kpis" / "9040.yaml", spec)
    write_yaml(extra_config / "models" / "reasons.yaml", _reasons_model())
    ctx = _dual_context(parquet_path, reasons, measures=["current_value"])
    with pytest.raises(BindError, match="cuts 'G157' is not on this extract"):
        compute(ctx, config_dir=extra_config)


def test_cross_model_fn_without_relations_fails(parquet_path, extra_config, tmp_path):
    """of/inputs that mix extracts need model_relations."""
    reasons = _write_reasons(tmp_path)
    spec = _dual_kpi()
    spec["measures"]["combo"] = {
        "op": "fn",
        "fn": "add",
        "inputs": ["current_value", "reason_count"],
    }
    write_yaml(extra_config / "kpis" / "9040.yaml", spec)
    write_yaml(extra_config / "models" / "reasons.yaml", _reasons_model())
    kpi = load_kpi(9040, extra_config)
    assert kpi.model_relations == ()
    with pytest.raises(BindError, match="spans models"):
        partition_request(kpi, ("combo",))
    ctx = _dual_context(parquet_path, reasons, measures=["combo"])
    with pytest.raises(BindError, match="spans models"):
        compute(ctx, config_dir=extra_config)


def test_156_ignore_does_not_strip_157_where(parquet_path, extra_config, tmp_path):
    """G156 ignore_filters cannot leak into a reasons-only extract."""
    reasons = _write_reasons(tmp_path)
    _write_dual_kpi(extra_config)
    ctx = _dual_context(
        parquet_path,
        reasons,
        measures=["reason_count"],
        extra_filters={"reason_code": {"values": ["LATE_SUPPLIER"], "input_text": "simple"}},
    )
    result = compute(ctx, config_dir=extra_config)
    assert "reason_code" in result["sql"]
    codes = {r["reason_code"] for r in result["rows"]}
    assert codes == {"LATE_SUPPLIER"}


def _write_dual_kpi(extra_config) -> None:
    """KPI 9040 plus the reasons extract model."""
    write_yaml(extra_config / "models" / "reasons.yaml", _reasons_model())
    write_yaml(extra_config / "kpis" / "9040.yaml", _dual_kpi())


def _reasons_model() -> dict:
    """Physical model for the reasons parquet."""
    return {
        "model_id": "reasons",
        "kind": "physical",
        "required_aliases": ["reasons"],
        "sources": {"reasons": {"alias": "reasons"}},
        "joins": [],
    }


def _dual_kpi() -> dict:
    """Sotif + reasons extracts, no model_relations, KPI-only cuts."""
    return {
        "kpi_id": 9040,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
        },
        "dimensions": [
            {"name": "region", "kind": "dimension"},
            {"name": "reason_code", "kind": "dimension"},
            {"name": "factor", "kind": "dimension"},
        ],
        "base_measures": {
            "shipped": {"sql": "amount", "agg": "sum"},
            "reason_1": {"sql": "qty", "agg": "sum", "model": "reasons"},
        },
        "cuts": [
            {
                "name": "G156",
                "group_by": ["region"],
                "ignore_filters": ["reason_code"],
                "also_emit": ["R156"],
            },
            {"name": "R156", "group_by": ["region"]},
            {"name": "G157", "group_by": ["reason_code", "factor"]},
        ],
        "default_cut": "G156",
        "measures": {
            "current_value": {"of": "shipped", "op": "point", "offset": {"months": 0}},
            "value_12m": {
                "of": "shipped",
                "op": "window",
                "trailing": {"months": 12},
                "inclusive": True,
            },
            "reason_count": {"of": "reason_1", "op": "point", "cuts": ["G157"]},
            "percent_gt": {
                "op": "percent_of_total",
                "of": "reason_count",
                "cuts": ["G157"],
            },
        },
    }


def _write_reasons(tmp_path) -> str:
    """Reasons fact: factor A in the anchor month, factor B only a year earlier."""
    path = tmp_path / "reasons.parquet"
    pd.DataFrame(
        [
            {
                "event_month": date(2026, 3, 1),
                "reason_code": "LATE_SUPPLIER",
                "factor": "A",
                "qty": 10,
            },
            {
                "event_month": date(2026, 3, 1),
                "reason_code": "OTHER",
                "factor": "A",
                "qty": 4,
            },
            {
                "event_month": date(2025, 3, 1),
                "reason_code": "LATE_SUPPLIER",
                "factor": "B",
                "qty": 99,
            },
        ]
    ).to_parquet(path, index=False)
    return str(path)


def _dual_context(
    parquet_path,
    reasons_path,
    *,
    measures: list[str],
    extra_filters: dict | None = None,
) -> dict:
    """Context with Sotif facts plus a reasons dataset."""
    return make_context(
        parquet_path,
        measures=measures,
        supplier=["ABC"],
        kpi_id=9040,
        extra_filters=extra_filters,
        extra_datasets={
            "Reasons": {
                "dataset_id": 41,
                "dataset_name": "REASONS",
                "table_type": "PARQUET",
                "path": str(reasons_path),
                "alias": "reasons",
                "columns": ["event_month", "reason_code", "factor", "qty"],
                "filter_column_mappings": [
                    {
                        "filter_id": 80,
                        "filter_code": "reason_code",
                        "view_id": 13,
                        "column_name": "reason_code",
                        "operator": "in",
                    }
                ],
            }
        },
    )
