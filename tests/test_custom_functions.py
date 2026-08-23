"""Custom-function tests: allowlisted YAML hooks, never importlib paths.

What this file provides
    Register a hook, reference it from KPI YAML, compute scalars that catalog
    ops cannot express (scale, blend). Unknown / dotted names fail at bind.

Where it is used
    pytest tests/test_custom_functions.py.

When to use
    Add a case when a new hook is registered for a production KPI.
"""

from datetime import date

import pandas as pd

from kpi_engine import compute
from kpi_engine.core.time_planner import lookback_for
from kpi_engine.core.binder import load_kpi
from kpi_engine.dates import add_months
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.core.hook_registry import REGISTRY, register, run, unregister
from tests.conftest import find_row, make_context, write_yaml


def _at(series: pd.DataFrame, time_col: str, measure: str, month: date) -> float | None:
    """Read one densified month from the hook's series argument."""
    hit = series[pd.to_datetime(series[time_col]) == pd.Timestamp(month)]
    if hit.empty:
        return None
    row = hit.iloc[0]
    if not bool(row.get("_observed", True)):
        return None
    value = row[measure]
    if pd.isna(value):
        return None
    return float(value)


def double_anchor(series, *, kpi, plan, spec, **_):
    """Custom: 2 × the base measure at the selected month."""
    value = _at(series, kpi.time.column, spec.of, plan.anchor)
    return None if value is None else value * 2


def blend_mom(series, *, kpi, plan, spec, **_):
    """Custom: 0.5 × current + 0.5 × previous calendar month."""
    current = _at(series, kpi.time.column, spec.of, plan.anchor)
    prior = _at(series, kpi.time.column, spec.of, add_months(plan.anchor, -1))
    if current is None or prior is None:
        return None
    return 0.5 * current + 0.5 * prior


def test_hook_registry_rejects_unknown_names():
    """run() only executes allowlisted names."""
    try:
        run("not_registered")
    except CatalogError as exc:
        assert "not_registered" in str(exc)
    else:
        raise AssertionError("expected CatalogError")


def test_yaml_hook_computes_custom_scalars(parquet_path, extra_config):
    """op: hook calls the registered function; lookback comes from offset/trailing."""
    register("double_anchor", double_anchor)
    register("blend_mom", blend_mom)
    try:
        write_yaml(extra_config / "kpis" / "9006.yaml", _hook_kpi(9006))
        kpi = load_kpi(9006, extra_config)
        by_key = {o.key: o for o in kpi.measures}
        assert lookback_for(by_key["doubled"], by_key) == 0
        assert lookback_for(by_key["blended"], by_key) == 1

        ctx = make_context(
            parquet_path,
            measures=["doubled", "blended", "current_value"],
            supplier=["ABC"],
            kpi_id=9006,
        )
        result = compute(ctx, config_dir=extra_config)
        g = find_row(result, cut="G", reason="LATE_SUPPLIER")
        assert g["current_value"] == 45.0
        assert g["doubled"] == 90.0
        # 0.5 * 45 + 0.5 * 30
        assert g["blended"] == 37.5
        assert result["parameters"]["lookback_months"] == 1
    finally:
        unregister("double_anchor")
        unregister("blend_mom")


def test_unknown_hook_fails_at_bind(extra_config):
    """YAML may not name a hook that is not in REGISTRY."""
    spec = _hook_kpi(9007, hook_name="never_registered")
    spec["measures"].pop("blended", None)
    write_yaml(extra_config / "kpis" / "9007.yaml", spec)
    try:
        load_kpi(9007, extra_config)
    except BindError as exc:
        assert "never_registered" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_dotted_import_path_is_not_a_hook(extra_config):
    """Hooks are allowlisted names, not context.udf.module_path / importlib strings."""
    spec = _hook_kpi(9008, hook_name="udfs.sotif.main")
    spec["measures"].pop("blended", None)
    write_yaml(
        extra_config / "kpis" / "9008.yaml",
        spec,
    )
    try:
        load_kpi(9008, extra_config)
    except BindError as exc:
        assert "udfs.sotif.main" in str(exc)
    else:
        raise AssertionError("expected BindError")
    assert "udfs.sotif.main" not in REGISTRY


def test_hook_without_name_fails(extra_config):
    """op: hook requires hook:."""
    spec = _hook_kpi(9009)
    spec["measures"]["doubled"] = {"op": "hook", "of": "sotif_value"}
    spec["measures"].pop("blended", None)
    write_yaml(extra_config / "kpis" / "9009.yaml", spec)
    try:
        load_kpi(9009, extra_config)
    except BindError as exc:
        assert "hook" in str(exc).lower()
    else:
        raise AssertionError("expected BindError")


def _hook_kpi(kpi_id: int, hook_name: str = "double_anchor") -> dict:
    """Minimal KPI that references named hooks."""
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "sotif",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "reason_code", "kind": "dimension"},
            {"name": "region", "kind": "dimension"},
        ],
        "base_measures": {"sotif_value": {"sql": "amount", "agg": "sum"}},
        "cuts": [
            {
                "name": "G",
                "group_by": ["reason_code"],
                "ignore_filters": ["region"],
                "also_emit": ["R"],
            },
            {"name": "R", "group_by": ["reason_code", "region"], "ignore_filters": []},
        ],
        "default_cut": "G",
        "row_set": "span_union",
        "measures": {
            "current_value": {"of": "sotif_value", "op": "point", "offset": {"months": 0}},
            "doubled": {"op": "hook", "hook": hook_name, "of": "sotif_value"},
            "blended": {
                "op": "hook",
                "hook": "blend_mom",
                "of": "sotif_value",
                "offset": {"months": 1},
            },
        },
    }
