"""Onboarding guards: every KPI/model YAML rule that must fail loudly.

What this file provides
    Binder validation for time, base_measures, cuts, measures, filter_map,
    model_relations, and model kinds — plus the positive parses that prove the
    guard is not over-tight.

Where it is used
    pytest tests/test_yaml_validation.py — no DuckDB.

When to use
    Add a case whenever KPI YAML gains a field or a new validation rule.
"""

import pytest

from kpi_engine.pipeline.binder import load_kpi, load_model
from kpi_engine.exceptions import BindError
from tests.conftest import minimal_kpi, write_yaml


def test_missing_kpi_and_model_files_are_reported_with_paths(extra_config):
    """A missing YAML names the file it looked for, so onboarding errors are obvious."""
    with pytest.raises(BindError, match="No KPI YAML for kpi_id=9998"):
        load_kpi(9998, extra_config)
    with pytest.raises(BindError, match="No model YAML for model_id='nope'"):
        load_model("nope", extra_config)


def test_yaml_must_contain_an_object(extra_config):
    """A YAML list or scalar is not a KPI definition."""
    path = extra_config / "kpis" / "9100.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(BindError, match="must contain a YAML object"):
        load_kpi(9100, extra_config)


def test_time_block_is_validated(extra_config):
    """time must be an object with a known grain and calendar."""
    _write(extra_config, 9101, time="month")
    with pytest.raises(BindError, match="time must be an object"):
        load_kpi(9101, extra_config)

    _write(extra_config, 9102, time=_time(grain="fortnight"))
    with pytest.raises(BindError, match="Unknown time.grain 'fortnight'"):
        load_kpi(9102, extra_config)

    _write(extra_config, 9103, time=_time(calendar="lunar"))
    with pytest.raises(BindError, match="Unknown time.calendar 'lunar'"):
        load_kpi(9103, extra_config)


def test_timezone_is_rejected_rather_than_silently_ignored(extra_config):
    """The engine buckets timestamps as stored, so a timezone would be a lie."""
    _write(extra_config, 9106, time=_time(timezone="Asia/Kolkata"))
    with pytest.raises(BindError, match="time.timezone is not supported"):
        load_kpi(9106, extra_config)


@pytest.mark.parametrize("start_month", [0, 13])
def test_fiscal_start_month_must_be_1_to_12(extra_config, start_month):
    """A fiscal calendar cannot start outside the year."""
    _write(extra_config, 9104, time=_time(calendar="fiscal", fiscal_start_month=start_month))
    with pytest.raises(BindError, match="fiscal_start_month must be 1-12"):
        load_kpi(9104, extra_config)


def test_fiscal_start_month_defaults_to_april(extra_config):
    """Omitting fiscal_start_month gives the April fiscal year the platform uses."""
    _write(extra_config, 9105, time=_time(calendar="fiscal", grain="quarter"))
    assert load_kpi(9105, extra_config).time.fiscal_start_month == 4


def test_omitted_calendar_defaults_to_gregorian(extra_config):
    """No calendar: key is gregorian; omitted fiscal_start_month is unused."""
    block = {
        "column": "event_month",
        "grain": "month",
        "filter_code": "reporting_month",
    }
    _write(extra_config, 91051, time=block)
    kpi = load_kpi(91051, extra_config)
    assert kpi.time.calendar == "gregorian"
    assert kpi.time.fiscal_start_month == 4


def test_fiscal_start_month_without_fiscal_calendar_is_bind_error(extra_config):
    """Explicit fiscal_start_month requires calendar: fiscal."""
    _write(extra_config, 91052, time=_time(fiscal_start_month=7))
    with pytest.raises(BindError, match="fiscal_start_month requires time.calendar: fiscal"):
        load_kpi(91052, extra_config)


def test_offset_unknown_key_is_bind_error(extra_config):
    """offset.year (singular) used to silently zero; it is now a BindError."""
    spec = minimal_kpi(91053)
    spec["measures"]["previous_year_value"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"year": 1},
    }
    write_yaml(extra_config / "kpis" / "91053.yaml", spec)
    with pytest.raises(BindError, match="unknown key"):
        load_kpi(91053, extra_config)


def test_offset_cannot_mix_periods_with_calendar_units(extra_config):
    spec = minimal_kpi(91054)
    spec["measures"]["prior"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"periods": 1, "months": 1},
    }
    write_yaml(extra_config / "kpis" / "91054.yaml", spec)
    with pytest.raises(BindError, match="cannot mix"):
        load_kpi(91054, extra_config)


def test_offset_periods_binds(extra_config):
    spec = minimal_kpi(91055)
    spec["measures"]["prior"] = {
        "of": "sotif_value",
        "op": "point",
        "offset": {"periods": 1},
    }
    write_yaml(extra_config / "kpis" / "91055.yaml", spec)
    kpi = load_kpi(91055, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert by_key["prior"].offset.periods == 1
    assert by_key["prior"].offset.months == 0


def test_trailing_unknown_key_is_bind_error(extra_config):
    spec = minimal_kpi(91056)
    spec["measures"]["win"] = {
        "of": "sotif_value",
        "op": "window",
        "trailing": {"month": 3},
    }
    write_yaml(extra_config / "kpis" / "91056.yaml", spec)
    with pytest.raises(BindError, match="unknown key"):
        load_kpi(91056, extra_config)


def test_base_measure_shape_and_agg_are_validated(extra_config):
    """base_measures entries are objects with an agg the extract knows."""
    _write(extra_config, 9106, base_measures={"sotif_value": "amount"})
    with pytest.raises(BindError, match="base_measures.sotif_value must be an object"):
        load_kpi(9106, extra_config)

    _write(extra_config, 9107, base_measures={"sotif_value": {"sql": "amount", "agg": "geomean"}})
    with pytest.raises(BindError, match="Unknown agg 'geomean'"):
        load_kpi(9107, extra_config)


def test_agg_defaults_to_sum(extra_config):
    """Omitting agg means SUM, the common case for onboarding."""
    _write(extra_config, 9108, base_measures={"sotif_value": {"sql": "amount"}})
    assert load_kpi(9108, extra_config).base_measures[0].agg == "sum"


@pytest.mark.parametrize(
    ("given", "expected"),
    [(90, 0.9), (0.9, 0.9), (99.5, 0.995), (0, 0.0), (100, 1.0)],
)
def test_percentile_accepts_fraction_or_percent(extra_config, given, expected):
    """percentile: 90 and percentile: 0.9 mean the same quantile."""
    _write(
        extra_config,
        9109,
        base_measures={"sotif_value": {"sql": "amount", "agg": "percentile", "percentile": given}},
    )
    assert load_kpi(9109, extra_config).base_measures[0].percentile == pytest.approx(expected)


def test_percentile_out_of_range_and_missing_are_rejected(extra_config):
    """A percentile must land in 0-1 (or 0-100), and agg=percentile requires one."""
    _write(
        extra_config,
        9110,
        base_measures={"sotif_value": {"sql": "amount", "agg": "percentile", "percentile": -5}},
    )
    with pytest.raises(BindError, match="percentile must be in 0-1 or 0-100"):
        load_kpi(9110, extra_config)

    _write(
        extra_config,
        9111,
        base_measures={"sotif_value": {"sql": "amount", "agg": "percentile"}},
    )
    with pytest.raises(BindError, match="agg=percentile requires percentile"):
        load_kpi(9111, extra_config)


def test_cuts_are_required_and_named(extra_config):
    """A KPI needs at least one cut, each cut is an object with a name."""
    _write(extra_config, 9112, cuts=[], default_cut="G")
    with pytest.raises(BindError, match="At least one cut is required"):
        load_kpi(9112, extra_config)

    _write(extra_config, 9113, cuts=["G"], default_cut="G")
    with pytest.raises(BindError, match="Each cut must be an object"):
        load_kpi(9113, extra_config)

    _write(extra_config, 9114, cuts=[{"group_by": ["reason_code"]}], default_cut="G")
    with pytest.raises(BindError, match="Cut name is required"):
        load_kpi(9114, extra_config)


def test_default_cut_must_be_declared(extra_config):
    """default_cut has to name one of the cuts."""
    _write(extra_config, 9115, default_cut="Z")
    with pytest.raises(BindError, match="default_cut 'Z' is not a declared cut"):
        load_kpi(9115, extra_config)


def test_default_cut_falls_back_to_the_first_cut(extra_config):
    """Omitting default_cut uses the first declared cut."""
    spec = minimal_kpi(9116)
    del spec["default_cut"]
    write_yaml(extra_config / "kpis" / "9116.yaml", spec)
    assert load_kpi(9116, extra_config).default_cut == "G"


def test_measures_cannot_be_empty(extra_config):
    """A KPI with no requestable measures cannot answer any context."""
    _write(extra_config, 9117, measures={})
    with pytest.raises(BindError, match="measures cannot be empty"):
        load_kpi(9117, extra_config)

    _write(extra_config, 9118, measures={"current_value": "point"})
    with pytest.raises(BindError, match="measures.current_value must be an object"):
        load_kpi(9118, extra_config)


def test_measure_accepts_kind_or_op_spelling(extra_config):
    """`kind:` and `op:` are interchangeable in measure YAML."""
    _write(
        extra_config,
        9119,
        measures={"current_value": {"of": "sotif_value", "kind": "point", "offset": {"months": 0}}},
    )
    assert load_kpi(9119, extra_config).measures[0].kind == "point"


def test_trailing_accepts_any_period_unit(extra_config):
    """trailing may be spelled in periods, months, days, quarters, or years."""
    for i, unit in enumerate(("periods", "months", "days", "quarters", "years")):
        kpi_id = 9120 + i
        _write(
            extra_config,
            kpi_id,
            measures={
                "window_value": {
                    "of": "sotif_value",
                    "op": "window",
                    "trailing": {unit: 4},
                    "inclusive": True,
                }
            },
        )
        spec = load_kpi(kpi_id, extra_config).measures[0]
        assert spec.key == "window_value"
        assert spec.trailing_months == 4


def test_model_relations_are_validated(extra_config):
    """model_relations need objects, real base measures, join keys, and a known how."""
    _write(extra_config, 9130, model_relations=["a-b"])
    with pytest.raises(BindError, match="model_relations entry must be an object"):
        load_kpi(9130, extra_config)

    _write(
        extra_config,
        9131,
        model_relations=[{"left": "sotif_value", "right": "sotif_value", "on": ["event_month"], "how": "cross"}],
    )
    with pytest.raises(BindError, match="model_relations.how 'cross'"):
        load_kpi(9131, extra_config)

    _write(
        extra_config,
        9132,
        model_relations=[{"left": "sotif_value", "right": "sotif_value", "on": []}],
    )
    with pytest.raises(BindError, match="model_relations.on must list join keys"):
        load_kpi(9132, extra_config)

    _write(
        extra_config,
        9133,
        model_relations=[{"left": "sotif_value", "right": "ghost_value", "on": ["event_month"]}],
    )
    with pytest.raises(BindError, match="must be base_measures names"):
        load_kpi(9133, extra_config)


def test_model_relations_full_outer_is_an_alias_for_outer(extra_config):
    """`full` and `full_outer` normalize to a pandas outer merge."""
    for i, how in enumerate(("full", "full_outer")):
        kpi_id = 9134 + i
        _write(
            extra_config,
            kpi_id,
            model_relations=[
                {"left": "sotif_value", "right": "sotif_value", "on": ["event_month"], "how": how}
            ],
        )
        assert load_kpi(kpi_id, extra_config).model_relations[0].how == "outer"


def test_filter_map_values_must_be_identifiers(extra_config):
    """filter_map points at a column name, never at an expression."""
    _write(extra_config, 9136, filter_map={"plant_code": "region = 'NA'"})
    with pytest.raises(BindError, match="Illegal filter_map column"):
        load_kpi(9136, extra_config)


def test_time_compose_template_is_validated(extra_config):
    """compose.template needs two `{filter}` placeholders and no nested braces."""
    _write(
        extra_config,
        9143,
        time=_time(compose={"template": "{year}"}),
    )
    with pytest.raises(BindError, match="at least two context filters"):
        load_kpi(9143, extra_config)

    _write(
        extra_config,
        9144,
        time=_time(compose={"template": "{year}{month:02}"}),
    )
    kpi = load_kpi(9144, extra_config)
    assert kpi.time.compose_template == "{year}{month:02}"

    _write(extra_config, 9145, time=_time(compose="year+month"))
    with pytest.raises(BindError, match="must be an object with template"):
        load_kpi(9145, extra_config)


def test_filters_block_parses_ops_and_rejects_bad_apply(extra_config):
    _write(
        extra_config,
        9140,
        filters={
            "plant": "reason_code",
            "effective_day": {
                "column": "amount",
                "op": "<=",
                "optional": True,
                "apply": "extract",
            },
        },
    )
    kpi = load_kpi(9140, extra_config)
    by_code = {s.code: s for s in kpi.filter_specs}
    assert by_code["plant"].column == "reason_code"
    assert by_code["plant"].op == "in"
    assert by_code["plant"].apply == "extract"
    assert by_code["effective_day"].op == "lte"
    assert by_code["effective_day"].optional is True

    _write(extra_config, 9141, filters={"region": {"column": "region", "apply": "scan"}})
    with pytest.raises(BindError, match="apply must be extract, calc, or result"):
        load_kpi(9141, extra_config)

    _write(
        extra_config,
        9142,
        filters={"amount": {"column": "amount", "op": "gt", "apply": "result"}},
    )
    with pytest.raises(BindError, match="apply: result must name a dimension column"):
        load_kpi(9142, extra_config)


def test_model_kind_is_validated(extra_config):
    """Models are physical or sql; nothing else is executable."""
    write_yaml(
        extra_config / "models" / "weird.yaml",
        {"model_id": "weird", "kind": "graph", "required_aliases": ["sotif"]},
    )
    with pytest.raises(BindError, match="Unknown model kind 'graph'"):
        load_model("weird", extra_config)


def test_physical_model_needs_aliases_or_sources(extra_config):
    """A physical model with nothing to scan cannot be bound to context datasets."""
    write_yaml(extra_config / "models" / "empty_physical.yaml", {"model_id": "empty_physical"})
    with pytest.raises(BindError, match="Physical models need required_aliases or sources"):
        load_model("empty_physical", extra_config)


def test_sql_model_needs_a_sql_block(extra_config):
    """kind: sql without sql: is a broken model."""
    write_yaml(
        extra_config / "models" / "empty_sql.yaml",
        {"model_id": "empty_sql", "kind": "sql", "required_aliases": ["sotif"]},
    )
    with pytest.raises(BindError, match="SQL models require a sql: block"):
        load_model("empty_sql", extra_config)


def test_source_alias_defaults_to_source_name(extra_config):
    """A source without an explicit alias is addressable by its own name."""
    write_yaml(
        extra_config / "models" / "implicit_alias.yaml",
        {"model_id": "implicit_alias", "kind": "physical", "sources": {"sotif": {}}},
    )
    model = load_model("implicit_alias", extra_config)
    assert model.required_aliases == ("sotif",)
    assert model.sources[0].alias == "sotif"


def test_default_paths_shape_is_validated(extra_config):
    """default_paths is alias → path (or alias → {path}); anything else fails."""
    write_yaml(
        extra_config / "models" / "bad_defaults.yaml",
        {
            "model_id": "bad_defaults",
            "kind": "physical",
            "required_aliases": ["sotif"],
            "default_paths": ["/tmp/x.parquet"],
        },
    )
    with pytest.raises(BindError, match="default_paths must be an object"):
        load_model("bad_defaults", extra_config)

    write_yaml(
        extra_config / "models" / "blank_default.yaml",
        {
            "model_id": "blank_default",
            "kind": "physical",
            "required_aliases": ["sotif"],
            "default_paths": {"sotif": ""},
        },
    )
    with pytest.raises(BindError, match="default_paths.sotif needs a path"):
        load_model("blank_default", extra_config)


def test_two_models_without_relations_load(extra_config):
    """A KPI may name two extracts; join is required only when a request spans them."""
    _write(
        extra_config,
        9150,
        base_measures={
            "sotif_value": {"sql": "amount", "agg": "sum"},
            "reason_1": {"sql": "qty", "agg": "sum", "model": "reasons"},
        },
        measures={
            "current_value": {"of": "sotif_value", "op": "point"},
            "reason_count": {"of": "reason_1", "op": "point"},
        },
    )
    kpi = load_kpi(9150, extra_config)
    assert {b.model_id or kpi.model_id for b in kpi.base_measures} == {"sotif", "reasons"}
    assert kpi.model_relations == ()


def test_cuts_and_dimensions_cannot_set_model(extra_config):
    """Cuts and dimensions stay on the KPI YAML; model: is extract-only."""
    _write(
        extra_config,
        9151,
        cuts=[{"name": "G", "group_by": ["region"], "model": "sotif"}],
    )
    with pytest.raises(BindError, match="cuts cannot set model"):
        load_kpi(9151, extra_config)
    _write(
        extra_config,
        9152,
        dimensions=[{"name": "region", "model": "sotif"}],
    )
    with pytest.raises(BindError, match="dimensions cannot set model"):
        load_kpi(9152, extra_config)


def test_blank_source_path_is_treated_as_missing(extra_config):
    """A whitespace-only YAML path must not shadow the context path."""
    write_yaml(
        extra_config / "models" / "blank_source.yaml",
        {
            "model_id": "blank_source",
            "kind": "physical",
            "sources": {"sotif": {"alias": "sotif", "path": "   "}},
        },
    )
    assert load_model("blank_source", extra_config).sources[0].default_path is None


def _time(**overrides) -> dict:
    """Default 3004-style time block with overrides applied."""
    block = {
        "column": "event_month",
        "grain": "month",
        "filter_code": "reporting_month",
        "calendar": "gregorian",
    }
    block.update(overrides)
    return block


def _write(extra_config, kpi_id: int, **overrides) -> None:
    """Write a test-only KPI YAML with the given top-level overrides."""
    write_yaml(extra_config / "kpis" / f"{kpi_id}.yaml", minimal_kpi(kpi_id, **overrides))


def test_where_between_requires_values_list_not_value(extra_config):
    """Arity-2 where: cannot use singular value: (F16)."""
    spec = minimal_kpi(9401)
    spec["base_measures"] = {
        "mid": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "amount", "op": "between", "value": 10},
        }
    }
    write_yaml(extra_config / "kpis" / "9401.yaml", spec)
    with pytest.raises(BindError, match="values: \\[lo, hi\\]"):
        load_kpi(9401, extra_config)


def test_where_rejects_like_even_if_pandas_mask_knows_it(extra_config):
    """Bind list is source of truth; like is not a base-measure where op (F17)."""
    spec = minimal_kpi(9402)
    spec["base_measures"] = {
        "mid": {
            "sql": "amount",
            "agg": "sum",
            "where": {"column": "reason_code", "op": "like", "value": "%LATE%"},
        }
    }
    write_yaml(extra_config / "kpis" / "9402.yaml", spec)
    with pytest.raises(BindError, match="where.op must be"):
        load_kpi(9402, extra_config)
