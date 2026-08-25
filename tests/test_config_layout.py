"""KPI / model YAML live under an optional one-level kpi_group folder.

What this file provides
    Lookup by globally unique kpi_id / model_id across flat and nested paths.
    Collision when the same id exists in two groups (or flat + nested).

Where it is used
    pytest tests/test_config_layout.py — no DuckDB.

When to use
    Add a case when the config folder layout or uniqueness rule changes.
"""

from __future__ import annotations

import pytest

from kpi_engine.core.binder import load_kpi, load_model
from kpi_engine.exceptions import BindError
from tests.conftest import minimal_kpi, write_yaml


def _physical_model(model_id: str, alias: str = "sotif") -> dict:
    """Minimal physical model that binds like sotif."""
    return {
        "model_id": model_id,
        "kind": "physical",
        "required_aliases": [alias],
        "sources": {alias: {"alias": alias}},
        "joins": [],
    }


def test_shipped_3004_loads_from_sotif_group(config_dir):
    """Gold files live under kpis/sotif/ and models/sotif/; ids are still 3004 / sotif."""
    kpi = load_kpi(3004, config_dir)
    assert kpi.kpi_id == 3004
    assert kpi.model_id == "sotif"
    model = load_model("sotif", config_dir)
    assert model.model_id == "sotif"


def test_nested_kpi_and_model_same_group(extra_config):
    """A new KPI and its model can share one authoring group folder."""
    write_yaml(extra_config / "models" / "quality" / "lane.yaml", _physical_model("lane"))
    write_yaml(
        extra_config / "kpis" / "quality" / "9610.yaml",
        minimal_kpi(9610, model="lane"),
    )
    kpi = load_kpi(9610, extra_config)
    assert kpi.model_id == "lane"
    assert load_model("lane", extra_config).model_id == "lane"


def test_kpi_in_one_group_can_use_model_in_another(extra_config):
    """model: is a globally unique id; the KPI group and model group need not match."""
    write_yaml(extra_config / "models" / "freight" / "orders.yaml", _physical_model("orders"))
    write_yaml(
        extra_config / "kpis" / "quality" / "9611.yaml",
        minimal_kpi(9611, model="orders"),
    )
    kpi = load_kpi(9611, extra_config)
    assert kpi.model_id == "orders"
    assert load_model("orders", extra_config).model_id == "orders"


def test_flat_kpi_path_still_works(extra_config):
    """Tests and one-off YAML may still sit directly under kpis/."""
    write_yaml(extra_config / "kpis" / "9612.yaml", minimal_kpi(9612))
    kpi = load_kpi(9612, extra_config)
    assert kpi.kpi_id == 9612


def test_duplicate_kpi_id_in_two_groups_is_an_error(extra_config):
    """kpi_id must be unique across groups (and vs a flat file)."""
    write_yaml(extra_config / "kpis" / "other" / "3004.yaml", minimal_kpi(3004))
    with pytest.raises(BindError, match="Multiple YAML files for kpi_id='3004'"):
        load_kpi(3004, extra_config)


def test_duplicate_model_id_in_two_groups_is_an_error(extra_config):
    """model_id must be unique across groups (and vs a flat file)."""
    write_yaml(extra_config / "models" / "other" / "sotif.yaml", _physical_model("sotif"))
    with pytest.raises(BindError, match="Multiple YAML files for model_id='sotif'"):
        load_model("sotif", extra_config)


def test_nested_model_name_fold(extra_config):
    """Sotif-style fold still applies one group down (Lane_Fact.yaml ↔ lane_fact)."""
    write_yaml(
        extra_config / "models" / "quality" / "Lane_Fact.yaml",
        _physical_model("lane_fact"),
    )
    model = load_model("lane_fact", extra_config)
    assert model.model_id == "lane_fact"


def test_missing_files_mention_flat_and_nested_paths(extra_config):
    """A miss names both the flat path and the group glob."""
    with pytest.raises(BindError, match=r"No KPI YAML for kpi_id=9998.*\*/9998\.yaml"):
        load_kpi(9998, extra_config)
    with pytest.raises(BindError, match=r"No model YAML for model_id='nope'.*\*/nope\.yaml"):
        load_model("nope", extra_config)
