"""Named row steps, lookup, over, having, and rollup bind/eval rules."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.core.binder import load_kpi
from kpi_engine.exceptions import BindError, CatalogError
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml


def _write(extra_config, kpi_id: int, **overrides):
    spec = minimal_kpi(kpi_id, **overrides)
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", spec)
    return spec


def test_named_row_steps_topo_and_cycle(parquet_path, extra_config):
    _write(
        extra_config,
        8801,
        base_measures={
            "gross": {"expr": "amount * 2"},
            "net": {"expr": "gross - 1", "agg": "sum"},
        },
        measures={"current_value": {"of": "net", "op": "point"}},
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8801)
    result = compute(ctx, config_dir=extra_config)
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    # March: NA 30 + EU 15; per-row (2*amount - 1) then sum.
    assert row["current_value"] == pytest.approx((2 * 30 - 1) + (2 * 15 - 1))

    with pytest.raises(BindError, match="cycle"):
        _write(
            extra_config,
            8802,
            base_measures={
                "a": {"expr": "b + 1"},
                "b": {"expr": "a + 1", "agg": "sum"},
            },
            measures={"current_value": {"of": "b", "op": "point"}},
        )
        load_kpi(8802, extra_config)


def test_helper_cannot_be_measure_of(extra_config):
    with pytest.raises(BindError, match="row helper"):
        _write(
            extra_config,
            8803,
            base_measures={"gross": {"expr": "amount * 2"}},
            measures={"current_value": {"of": "gross", "op": "point"}},
        )
        load_kpi(8803, extra_config)


def test_lookup_default_and_strict(tmp_path, extra_config):
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1), date(2026, 3, 1)],
            "reason_code": ["LATE_SUPPLIER", "LATE_SUPPLIER"],
            "region": ["NA", "NA"],
            "supplier_name": ["ABC", "ABC"],
            "amount": [10.0, 20.0],
            "pay": ["COD", "UPI"],
        }
    )
    path = tmp_path / "lookup.parquet"
    frame.to_parquet(path, index=False)
    _write(
        extra_config,
        8804,
        base_measures={
            "fee": {
                "lookup": {"column": "pay", "map": {"COD": 25}, "default": 10},
                "agg": "sum",
            }
        },
        measures={"current_value": {"of": "fee", "op": "point"}},
    )
    ctx = make_context(path, measures=["current_value"], kpi_id=8804)
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    result = compute(ctx, config_dir=extra_config)
    row = next(r for r in result["rows"] if r["output_cut"] == "G")
    assert row["current_value"] == pytest.approx(35.0)

    _write(
        extra_config,
        8805,
        base_measures={
            "fee": {
                "lookup": {"column": "pay", "map": {"COD": 25}, "strict": True},
                "agg": "sum",
            }
        },
        measures={"current_value": {"of": "fee", "op": "point"}},
    )
    ctx = make_context(path, measures=["current_value"], kpi_id=8805)
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    with pytest.raises(CatalogError, match="unknown key"):
        compute(ctx, config_dir=extra_config)


def test_window_sum_is_bind_error_unless_agg_ok(extra_config):
    with pytest.raises(BindError, match="cannot use agg='sum'"):
        _write(
            extra_config,
            8806,
            dimensions=[
                {"name": "reason_code", "from": "reason_code"},
                {"name": "region", "from": "region"},
                {"name": "supplier", "from": "supplier_name"},
            ],
            base_measures={
                "running": {
                    "over": {
                        "fn": "running_sum",
                        "of": "amount",
                        "partition_by": ["region"],
                        "order_by": ["event_month"],
                    },
                    "agg": "sum",
                }
            },
            measures={"current_value": {"of": "running", "op": "point"}},
        )
        load_kpi(8806, extra_config)


def test_name_clash_needs_replace(parquet_path, extra_config):
    _write(
        extra_config,
        8807,
        base_measures={"amount": {"expr": "amount * 2", "agg": "sum", "replace": True}},
        measures={"current_value": {"of": "amount", "op": "point"}},
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8807)
    result = compute(ctx, config_dir=extra_config)
    assert any(w.get("reason") == "replace_extract_column" for w in result["grain_warnings"])
    row = find_row(result, cut="G", reason="LATE_SUPPLIER")
    assert row["current_value"] == pytest.approx(2 * (30 + 15))

    _write(
        extra_config,
        8808,
        base_measures={"amount": {"expr": "amount * 2", "agg": "sum"}},
        measures={"current_value": {"of": "amount", "op": "point"}},
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8808)
    with pytest.raises(CatalogError, match="overwrite extract column"):
        compute(ctx, config_dir=extra_config)


def test_calendar_lag_rejects_partition_by(extra_config):
    with pytest.raises(BindError, match="partition_by"):
        _write(
            extra_config,
            8809,
            measures={
                "current_value": {"of": "sotif_value", "op": "point"},
                "prev": {
                    "op": "lag",
                    "of": "sotif_value",
                    "offset": {"months": 1},
                    "partition_by": ["region"],
                },
            },
        )
        load_kpi(8809, extra_config)


def test_having_drops_and_predicate_flags(parquet_path, extra_config):
    _write(
        extra_config,
        8810,
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "clears": {
                "op": "predicate",
                "match": "all",
                "predicates": [{"of": "current_value", "cmp": "gt", "value": 1000}],
            },
        },
        having={
            "match": "all",
            "predicates": [{"of": "current_value", "cmp": "gt", "value": 1000}],
        },
    )
    ctx = make_context(parquet_path, measures=["current_value", "clears"], kpi_id=8810)
    flagged = compute(ctx, config_dir=extra_config)
    # without requesting having-only... having is on the KPI so rows drop
    assert flagged["rows"] == []
    assert flagged["dropped_groups"]
    assert flagged["pagination"]["total_count"] == 0


def test_having_g_vs_r_independent(parquet_path, extra_config):
    _write(
        extra_config,
        8811,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
        having={
            "predicates": [{"of": "current_value", "cmp": "gt", "value": 40}],
        },
    )
    ctx = make_context(
        parquet_path, measures=["current_value"], kpi_id=8811, region=["NA", "EU"]
    )
    result = compute(ctx, config_dir=extra_config)
    g = [r for r in result["rows"] if r["output_cut"] == "G"]
    r = [r for r in result["rows"] if r["output_cut"] == "R"]
    assert g, "worldwide can still clear a floor that regions miss"
    assert result["dropped_groups"]


def test_over_caps(monkeypatch, parquet_path, extra_config):
    monkeypatch.setattr("kpi_engine.core.row_pipeline.OVER_ROW_CAP", 1)
    _write(
        extra_config,
        8812,
        base_measures={
            "seq": {
                "over": {
                    "fn": "row_number",
                    "partition_by": ["region"],
                    "order_by": ["event_month"],
                },
                "agg": "max",
            }
        },
        measures={"current_value": {"of": "seq", "op": "point"}},
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8812)
    with pytest.raises(CatalogError, match="exceeds 1"):
        compute(ctx, config_dir=extra_config)


def test_date_diff_null_and_tz(tmp_path, extra_config):
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1), date(2026, 3, 1)],
            "reason_code": ["LATE_SUPPLIER", "LATE_SUPPLIER"],
            "region": ["NA", "NA"],
            "supplier_name": ["ABC", "ABC"],
            "amount": [1.0, 1.0],
            "start_date": [date(2026, 3, 1), None],
            "end_date": [date(2026, 3, 5), date(2026, 3, 5)],
        }
    )
    path = tmp_path / "dates.parquet"
    frame.to_parquet(path, index=False)
    _write(
        extra_config,
        8813,
        base_measures={
            "gap": {"expr": "date_diff(start_date, end_date, 'day')", "agg": "sum"},
        },
        measures={"current_value": {"of": "gap", "op": "point"}},
    )
    ctx = make_context(path, measures=["current_value"], kpi_id=8813)
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    result = compute(ctx, config_dir=extra_config)
    row = next(r for r in result["rows"] if r["output_cut"] == "G")
    assert row["current_value"] == pytest.approx(4.0)


def test_validate_still_ok(parquet_path, extra_config):
    _write(extra_config, 8814)
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8814)
    planned = validate(ctx, config_dir=extra_config)
    assert planned["ok"] is True


def test_last_n_lists_in_partition_order(tmp_path, extra_config):
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1)] * 3,
            "reason_code": ["LATE_SUPPLIER"] * 3,
            "region": ["NA", "NA", "NA"],
            "supplier_name": ["ABC"] * 3,
            "amount": [1.0, 2.0, 4.0],
            "order_id": ["a", "b", "c"],
        }
    )
    path = tmp_path / "lastn.parquet"
    frame.to_parquet(path, index=False)
    _write(
        extra_config,
        8815,
        dimensions=[
            {"name": "reason_code", "from": "reason_code"},
            {"name": "region", "from": "region"},
            {"name": "order_id", "from": "order_id"},
        ],
        default_dimensions=["order_id"],
        cuts=[{"name": "G", "group_by": [], "ignore_filters": []}],
        base_measures={
            "recent": {
                "over": {
                    "fn": "last_n",
                    "of": "amount",
                    "n": 2,
                    "partition_by": ["region"],
                    "order_by": ["order_id"],
                },
                "agg": "last",
            }
        },
        measures={"current_value": {"of": "recent", "op": "point"}},
    )
    ctx = make_context(
        path,
        measures=["current_value"],
        kpi_id=8815,
        selected_dimensions=["order_id"],
    )
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    result = compute(ctx, config_dir=extra_config)
    by_id = {row["order_id"]: row["current_value"] for row in result["rows"]}
    assert by_id["a"] == [1.0]
    assert by_id["b"] == [1.0, 2.0]
    assert by_id["c"] == [2.0, 4.0]


def test_then_group_by_avg_is_not_mean_of_avgs(tmp_path, extra_config):
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1)] * 3,
            "reason_code": ["LATE_SUPPLIER"] * 3,
            "region": ["NA", "NA", "EU"],
            "supplier_name": ["ABC"] * 3,
            "amount": [10.0, 10.0, 40.0],
        }
    )
    path = tmp_path / "rollup.parquet"
    frame.to_parquet(path, index=False)
    _write(
        extra_config,
        8816,
        default_dimensions=["reason_code", "region"],
        cuts=[{"name": "G", "group_by": [], "ignore_filters": []}],
        base_measures={"sotif_value": {"sql": "amount", "agg": "avg"}},
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
        having={
            "predicates": [{"of": "current_value", "cmp": "gt", "value": 0}],
            "then_group_by": ["reason_code"],
        },
    )
    ctx = make_context(
        path,
        measures=["current_value"],
        kpi_id=8816,
        selected_dimensions=["reason_code", "region"],
    )
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    result = compute(ctx, config_dir=extra_config)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["reason_code"] == "LATE_SUPPLIER"
    assert row["region"] is None
    assert row["current_value"] == pytest.approx(20.0)
    mean_of_avgs = (10.0 + 40.0) / 2
    assert row["current_value"] != pytest.approx(mean_of_avgs)


def test_two_model_over_is_bind_error(extra_config):
    write_yaml(
        extra_config / "models" / "marketing.yaml",
        {
            "model_id": "marketing",
            "kind": "physical",
            "required_aliases": ["marketing"],
            "sources": {"marketing": {"alias": "marketing"}},
            "joins": [],
        },
    )
    _write(
        extra_config,
        8817,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum", "model": "sotif"},
            "marketing_spend": {"sql": "spend", "agg": "sum", "model": "marketing"},
            "seq": {
                "over": {
                    "fn": "row_number",
                    "partition_by": ["marketing_spend"],
                    "order_by": ["event_month"],
                },
                "agg": "max",
                "model": "sotif",
            },
        },
        measures={"current_value": {"of": "seq", "op": "point"}},
    )
    with pytest.raises(BindError, match="not this extract"):
        load_kpi(8817, extra_config)


def test_tz_aware_date_diff_is_catalog_error(tmp_path, extra_config):
    stamp = pd.Timestamp("2026-03-01", tz="UTC")
    frame = pd.DataFrame(
        {
            "event_month": [date(2026, 3, 1), date(2026, 3, 1)],
            "reason_code": ["LATE_SUPPLIER", "LATE_SUPPLIER"],
            "region": ["NA", "NA"],
            "supplier_name": ["ABC", "ABC"],
            "amount": [1.0, 1.0],
            "start_date": [stamp, stamp],
            "end_date": [stamp, stamp],
        }
    )
    path = tmp_path / "tz.parquet"
    frame.to_parquet(path, index=False)
    _write(
        extra_config,
        8818,
        base_measures={
            "gap": {"expr": "date_diff(start_date, end_date, 'day')", "agg": "sum"},
        },
        measures={"current_value": {"of": "gap", "op": "point"}},
    )
    ctx = make_context(path, measures=["current_value"], kpi_id=8818)
    ctx["datasets"]["Sotif"]["columns"] = list(frame.columns)
    with pytest.raises(CatalogError, match="tz-naive"):
        compute(ctx, config_dir=extra_config)


def test_green_and_paginate_see_only_survivors(parquet_path, extra_config):
    _write(
        extra_config,
        8819,
        measures={"current_value": {"of": "sotif_value", "op": "point"}},
        having={"predicates": [{"of": "current_value", "cmp": "gt", "value": 1000}]},
        green_when={"of": "current_value", "above": 0},
    )
    ctx = make_context(
        parquet_path,
        measures=["current_value"],
        kpi_id=8819,
        page=1,
        page_size=10,
    )
    result = compute(ctx, config_dir=extra_config)
    assert result["rows"] == []
    assert result["pagination"]["total_count"] == 0
    assert result["dropped_groups"]
    assert all(item["reason"] == "having" for item in result["dropped_groups"])


def test_agg_ok_warns_and_computes(parquet_path, extra_config):
    _write(
        extra_config,
        8820,
        base_measures={
            "running": {
                "over": {
                    "fn": "running_sum",
                    "of": "amount",
                    "partition_by": ["region"],
                    "order_by": ["event_month"],
                },
                "agg": "sum",
                "agg_ok": True,
            }
        },
        measures={"current_value": {"of": "running", "op": "point"}},
    )
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=8820)
    result = compute(ctx, config_dir=extra_config)
    assert any(w.get("reason") == "window_agg_ok" for w in result["grain_warnings"])
    assert find_row(result, cut="G", reason="LATE_SUPPLIER")["current_value"] is not None
