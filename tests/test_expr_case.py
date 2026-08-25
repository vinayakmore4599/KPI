"""CASE, IS NULL, and allowlisted calls in expr: and as named functions."""

from __future__ import annotations

import pandas as pd
import pytest

from kpi_engine import compute
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import parse_expression
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _fact(tmp_path, rows) -> str:
    path = tmp_path / "case.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return str(path)


def _row(*, amount=10.0, status="O", extra=None, month="2026-03-01"):
    item = {
        "event_month": month,
        "region": "NA",
        "reason_code": "LATE_SUPPLIER",
        "supplier_name": "ABC",
        "amount": amount,
        "status": status,
    }
    if extra:
        item.update(extra)
    return item


def test_unknown_expr_function_fails_at_bind(extra_config):
    spec = minimal_kpi(9801)
    spec["base_measures"]["sotif_value"] = {"expr": "nope(amount)", "agg": "sum"}
    write_yaml(extra_config / "kpis" / "9801.yaml", spec)
    with pytest.raises(BindError, match="unknown function"):
        load_kpi(9801, extra_config)


def test_incomplete_case_fails_at_bind(extra_config):
    spec = minimal_kpi(9802)
    spec["base_measures"]["sotif_value"] = {
        "expr": "CASE WHEN amount THEN 1",
        "agg": "sum",
    }
    write_yaml(extra_config / "kpis" / "9802.yaml", spec)
    with pytest.raises(BindError, match="END"):
        load_kpi(9802, extra_config)


def test_comment_still_illegal():
    with pytest.raises(BindError, match="Illegal"):
        parse_expression("amount -- x", what="measure sql")


def test_one_arg_nullif_fails_at_bind(extra_config):
    spec = minimal_kpi(9803)
    spec["base_measures"]["sotif_value"] = {
        "columns": ["amount"],
        "op": "nullif",
        "agg": "sum",
    }
    write_yaml(extra_config / "kpis" / "9803.yaml", spec)
    with pytest.raises(BindError, match="at least 2"):
        load_kpi(9803, extra_config)


def test_zero_if_null_column_and_case_status(tmp_path, extra_config):
    path = _fact(
        tmp_path,
        [
            _row(amount=float("nan"), status="O"),
            _row(amount=5.0, status="O"),
            _row(amount=7.0, status="F"),
        ],
    )
    spec = minimal_kpi(
        9804,
        measures={
            "filled": {"of": "filled_fact", "op": "point", "offset": {"months": 0}},
            "open_amt": {"of": "open_fact", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "filled_fact": {"columns": ["amount"], "op": "zero_if_null", "agg": "sum"},
        "open_fact": {
            "expr": "CASE WHEN status = 'O' THEN amount ELSE 0 END",
            "agg": "sum",
        },
    }
    write_yaml(extra_config / "kpis" / "9804.yaml", spec)
    ctx = make_context(path, measures=["filled", "open_amt"], supplier=["ABC"], kpi_id=9804)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "status",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["filled"] == 12.0
    assert row["open_amt"] == 5.0


def test_null_if_zero_and_if_else_column(tmp_path, extra_config):
    path = _fact(
        tmp_path,
        [
            _row(amount=4.0, extra={"flag": 1, "fallback": 9.0}),
            _row(amount=0.0, extra={"flag": 0, "fallback": 9.0}),
        ],
    )
    spec = minimal_kpi(
        9805,
        measures={
            "nonzero": {"of": "nz", "op": "point", "offset": {"months": 0}},
            "picked": {"of": "pick", "op": "point", "offset": {"months": 0}},
        },
    )
    spec["base_measures"] = {
        "nz": {"columns": ["amount"], "op": "null_if_zero", "agg": "sum"},
        "pick": {
            "columns": {"cond": "flag", "then": "amount", "other": "fallback"},
            "op": "if_else",
            "agg": "sum",
        },
    }
    write_yaml(extra_config / "kpis" / "9805.yaml", spec)
    ctx = make_context(path, measures=["nonzero", "picked"], supplier=["ABC"], kpi_id=9805)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "status",
        "flag",
        "fallback",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["nonzero"] == 4.0
    assert row["picked"] == 13.0


def test_measure_expr_case_and_zero_if_null(tmp_path, extra_config):
    path = _fact(tmp_path, [_row(amount=3.0)])
    spec = minimal_kpi(
        9806,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "previous_year_value": {
                "of": "sotif_value",
                "op": "point",
                "offset": {"years": 1},
            },
            "target": {"op": "constant", "value": 10},
            "shown": {"op": "expr", "expr": "zero_if_null(previous_year_value)"},
            "blended": {
                "op": "expr",
                "expr": "CASE WHEN previous_year_value IS NULL THEN target ELSE previous_year_value END",
            },
        },
    )
    spec["base_measures"] = {"sotif_value": {"sql": "amount", "agg": "sum"}}
    write_yaml(extra_config / "kpis" / "9806.yaml", spec)
    ctx = make_context(
        path,
        measures=["shown", "blended", "previous_year_value"],
        supplier=["ABC"],
        kpi_id=9806,
    )
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "status",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["previous_year_value"] is None
    assert row["shown"] == 0.0
    assert row["blended"] == 10.0


def test_measure_fn_zero_if_null_and_if_else(tmp_path, extra_config):
    path = _fact(tmp_path, [_row(amount=8.0)])
    spec = minimal_kpi(
        9807,
        measures={
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "target": {"op": "constant", "value": 10},
            "flag": {"op": "constant", "value": 0},
            "shown": {"op": "fn", "fn": "zero_if_null", "inputs": ["current_value"]},
            "picked": {
                "op": "fn",
                "fn": "if_else",
                "inputs": {"cond": "flag", "then": "current_value", "other": "target"},
            },
        },
    )
    write_yaml(extra_config / "kpis" / "9807.yaml", spec)
    ctx = make_context(path, measures=["shown", "picked"], supplier=["ABC"], kpi_id=9807)
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["shown"] == 8.0
    assert row["picked"] == 10.0


def test_expr_nullif_zero(tmp_path, extra_config):
    path = _fact(tmp_path, [_row(amount=0.0), _row(amount=2.0)])
    spec = minimal_kpi(
        9808,
        measures={"current_value": {"of": "nz", "op": "point", "offset": {"months": 0}}},
    )
    spec["base_measures"] = {"nz": {"expr": "nullif(amount, 0)", "agg": "sum"}}
    write_yaml(extra_config / "kpis" / "9808.yaml", spec)
    ctx = make_context(path, measures=["current_value"], supplier=["ABC"], kpi_id=9808)
    ctx["datasets"]["Sotif"]["columns"] = [
        "event_month",
        "region",
        "reason_code",
        "supplier_name",
        "amount",
        "status",
    ]
    row = find_row(
        compute(ctx, config_dir=extra_config),
        cut="R",
        reason="LATE_SUPPLIER",
        region="NA",
    )
    assert row["current_value"] == 2.0
