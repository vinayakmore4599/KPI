"""Phase 3 WS9: opt-in multi_view envelope, partial failure, same kpi_id only."""

from __future__ import annotations

import pytest

from kpi_engine import compute
from kpi_engine.exceptions import BindError, ContextError
from kpi_engine.pipeline.adapter import adapt
from tests.conftest import make_context, minimal_kpi, write_yaml


def test_multi_view_returns_envelope(parquet_path, extra_config):
    spec = minimal_kpi(
        98601,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98601.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98601
    )
    ctx["execution"]["multi_view"] = True
    ctx["execution"]["view_details"] = [
        {"view_id": "a", "measures_required": ["current_value"]},
        {"view_id": "b", "measures_required": ["current_value"]},
    ]
    result = compute(ctx, config_dir=extra_config)
    assert result["multi_view"] is True
    assert len(result["views"]) == 2
    assert all(item["ok"] for item in result["views"])
    assert result["views"][0]["view_id"] == "a"
    assert "rows" in result["views"][0]["result"]


def test_multi_view_partial_failure(parquet_path, extra_config):
    spec = minimal_kpi(
        98602,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98602.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98602
    )
    ctx["execution"]["multi_view"] = True
    ctx["execution"]["view_details"] = [
        {"view_id": "ok", "measures_required": ["current_value"]},
        {"view_id": "bad", "measures_required": ["not_a_measure"]},
    ]
    result = compute(ctx, config_dir=extra_config)
    by_id = {item["view_id"]: item for item in result["views"]}
    assert by_id["ok"]["ok"] is True
    assert by_id["bad"]["ok"] is False
    assert by_id["bad"]["error"]["type"] == "BindError"


def test_multi_view_fail_fast(parquet_path, extra_config):
    spec = minimal_kpi(
        98603,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98603.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98603
    )
    ctx["execution"]["multi_view"] = True
    ctx["execution"]["fail_fast"] = True
    ctx["execution"]["view_details"] = [
        {"view_id": "ok", "measures_required": ["current_value"]},
        {"view_id": "bad", "measures_required": ["not_a_measure"]},
    ]
    with pytest.raises(BindError):
        compute(ctx, config_dir=extra_config)


def test_multi_view_rejects_different_kpi_id(parquet_path, extra_config):
    spec = minimal_kpi(
        98604,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
    )
    write_yaml(extra_config / "kpis" / "98604.yaml", spec)
    ctx = make_context(
        parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=98604
    )
    ctx["execution"]["multi_view"] = True
    ctx["execution"]["view_details"] = [
        {"view_id": "a", "measures_required": ["current_value"], "kpi_id": 98604},
        {"view_id": "b", "measures_required": ["current_value"], "kpi_id": 1},
    ]
    result = compute(ctx, config_dir=extra_config)
    by_id = {item["view_id"]: item for item in result["views"]}
    assert by_id["b"]["ok"] is False
    assert "one kpi_id" in by_id["b"]["error"]["message"]


def test_adapt_still_requires_one_view_without_opt_in(parquet_path):
    ctx = make_context(parquet_path, measures=["current_value"])
    ctx["execution"]["view_details"].append(
        {"view_id": 2, "measures_required": []}
    )
    with pytest.raises(ContextError, match="exactly one view"):
        adapt(ctx)
