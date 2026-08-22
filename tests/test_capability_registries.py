"""Capability registries: load, enable, discover, import ban."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpi_engine.core.binder import load_kpi
from kpi_engine.core.loader import (
    assert_named_capability,
    ensure_loaded,
    generate_capabilities_markdown,
    impact_check,
    list_capabilities,
    registries_dir,
    reload_packaged,
    skipped_addons,
    write_generated_docs,
)
from kpi_engine.core.op_registry import OP_KINDS, enabled_op_names, get_op
from kpi_engine.exceptions import BindError, CatalogError
from tests.conftest import write_yaml


FORBIDDEN_IMPORTS = ("binder", "calc_engine", "orchestrator", "model_sql", "filters")
CAPABILITIES_ROOT = Path(__file__).resolve().parents[1] / "udfs" / "kpi_engine" / "capabilities"


def test_packaged_registries_boot():
    reload_packaged()
    names = {row["name"] for row in list_capabilities() if row["type"] == "op"}
    assert {"point", "window", "rank", "percent_of_total", "fn"} <= names
    assert "point" in enabled_op_names()
    assert get_op("percent_of_total").phase == "cut"
    column = {row["name"] for row in list_capabilities() if row["type"] == "column_fn"}
    measure = {row["name"] for row in list_capabilities() if row["type"] == "measure_fn"}
    assert "divide" in column and "divide" in measure


def test_list_capabilities_includes_disabled_from_yaml():
    rows = list_capabilities()
    assert all("description" in row and "example" in row for row in rows)
    assert all(row["enabled"] is True for row in rows if row["role"] == "platform")


def test_column_and_measure_divide_both_register():
    from kpi_engine.core.fn_apply import COLUMN_FNS, MEASURE_FNS

    reload_packaged()
    assert "divide" in COLUMN_FNS
    assert "divide" in MEASURE_FNS
    assert COLUMN_FNS["divide"] is not MEASURE_FNS["divide"]


def test_platform_enabled_false_is_loader_error():
    with pytest.raises(CatalogError, match="cannot have enabled: false"):
        ensure_loaded(
            [
                {
                    "type": "op",
                    "name": "point",
                    "role": "platform",
                    "enabled": False,
                    "aliases": (),
                    "description": "x",
                    "example": "op: point",
                    "module": "kpi_engine.capabilities.ops.combo",
                    "attr": "Point",
                    "source": "test",
                }
            ]
        )
    reload_packaged()


def test_addon_bad_module_does_not_block_import():
    ensure_loaded(
        [
            {
                "type": "op",
                "name": "point",
                "role": "platform",
                "enabled": True,
                "aliases": (),
                "description": "x",
                "example": "op: point",
                "module": "kpi_engine.capabilities.ops.combo",
                "attr": "Point",
                "source": "test",
            },
            {
                "type": "op",
                "name": "broken_addon",
                "role": "addon",
                "enabled": True,
                "aliases": (),
                "description": "x",
                "example": "op: broken_addon",
                "module": "kpi_engine.capabilities.does_not_exist",
                "attr": "Nope",
                "source": "test",
            },
        ]
    )
    assert "broken_addon" in skipped_addons()
    assert "point" in OP_KINDS
    with pytest.raises(BindError, match="failed to load"):
        assert_named_capability("op", "broken_addon", what="measures.x")
    reload_packaged()


def test_module_prefix_rejected():
    ensure_loaded(
        [
            {
                "type": "column_fn",
                "name": "evil",
                "role": "addon",
                "enabled": True,
                "aliases": (),
                "description": "x",
                "example": "op: evil",
                "module": "os.path",
                "attr": "join",
                "source": "test",
            }
        ]
    )
    assert "evil" in skipped_addons()
    assert "must start with" in skipped_addons()["evil"]
    reload_packaged()


def test_kpi_yaml_cannot_name_a_module_path(extra_config):
    write_yaml(
        extra_config / "kpis" / "9898.yaml",
        {
            "kpi_id": 9898,
            "model": "sotif",
            "time": {"column": "event_month", "grain": "month", "filter_code": "reporting_month"},
            "dimensions": ["region"],
            "base_measures": {"v": {"sql": "amount", "agg": "sum"}},
            "cuts": [{"name": "G", "group_by": []}],
            "measures": {
                "bad": {"op": "kpi_engine.capabilities.ops.combo.Point", "of": "v"},
            },
        },
    )
    with pytest.raises(BindError, match="unknown op/kind"):
        load_kpi(9898, extra_config)


def test_unknown_key_on_point_is_bind_error(extra_config):
    write_yaml(
        extra_config / "kpis" / "9899.yaml",
        {
            "kpi_id": 9899,
            "model": "sotif",
            "time": {"column": "event_month", "grain": "month", "filter_code": "reporting_month"},
            "dimensions": ["region"],
            "base_measures": {"v": {"sql": "amount", "agg": "sum"}},
            "cuts": [{"name": "G", "group_by": []}],
            "measures": {
                "cur": {"op": "point", "of": "v", "tiles": 4},
            },
        },
    )
    with pytest.raises(BindError, match="does not accept 'tiles'"):
        load_kpi(9899, extra_config)


def test_facade_modules_do_not_import_calc_engine():
    import kpi_engine.capabilities.ops.support as support
    import kpi_engine.contracts as contracts
    import kpi_engine.core.op_protocol as protocol

    for module in (protocol, contracts, support):
        source = Path(module.__file__).read_text()
        assert "import kpi_engine.core.calc_engine" not in source
        assert "from kpi_engine.core.calc_engine" not in source


def test_capabilities_do_not_import_core_engines():
    banned = []
    for path in CAPABILITIES_ROOT.rglob("*.py"):
        text = path.read_text()
        for name in FORBIDDEN_IMPORTS:
            if f"kpi_engine.core.{name}" in text:
                banned.append(f"{path.name} imports {name}")
    assert banned == []


def test_impact_check_finds_percent_of_total():
    hits = impact_check("percent_of_total")
    assert hits, "expected at least one shipped KPI YAML to name percent_of_total"


def test_generated_catalog_mentions_point():
    text = generate_capabilities_markdown()
    assert "`point`" in text
    assert "Column functions" in text
    dest = write_generated_docs()
    assert dest.exists()
    assert "point" in dest.read_text()


def test_registries_dir_has_four_files():
    root = registries_dir()
    assert (root / "functions" / "column.yaml").is_file()
    assert (root / "functions" / "measure.yaml").is_file()
    assert (root / "ops.yaml").is_file()
    assert (root / "hooks.yaml").is_file()
