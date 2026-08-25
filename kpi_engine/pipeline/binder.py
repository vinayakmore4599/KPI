"""Load KPI/model YAML and bind context datasets by alias.

What this file provides
    default_config_dir, load_kpi, load_model, bind_datasets, assert_measure_keys,
    fold_measure_keys, resolve_requested_graph, same_model_id. Parsers for cuts,
    measures, models.

Where it is used
    orchestrator after adapt(). Tests load KPI 3004 via load_kpi.

Capabilities
    - Reads kpi_config/kpis/<kpi_id>.yaml or kpi_config/kpis/<kpi_group>/<kpi_id>.yaml.
    - Reads kpi_config/models/<model_id>.yaml or kpi_config/models/<kpi_group>/<model_id>.yaml.
    - kpi_id / model_id are globally unique; the group folder is authoring only.
    - Validates identifiers, aggs, default_cut, measure keys.
    - `base_measures.sql` / `expr:` / `columns:` + `op` run in Pandas after retrieve.
    - DuckDB never receives KPI YAML formulas.
    - Binds model.required_aliases to context.datasets by alias, then key.
    - Context path wins; model default_path / default_paths fills a missing alias.
    - Unknown measure_key is a hard error listing valid YAML keys.
    - resolve_requested_graph scopes work to measures_required plus dependencies.

When to use
    Change parsing when YAML schema changes (new measure op, new cut field).
    To onboard a KPI, add a YAML file — do not edit this module.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from collections.abc import Mapping
from typing import Any

import yaml

from kpi_engine.contracts import (
    GRAIN_NAMES,
    OVER_FNS,
    AdaptedRequest,
    BaseMeasure,
    CutSpec,
    DatasetBinding,
    DimensionSpec,
    FilterApplySpec,
    GreenWhen,
    HavingSpec,
    JoinSpec,
    KpiMeta,
    KpiSpec,
    LookupSpec,
    MeasureWhere,
    ModelRelation,
    ModelSpec,
    Offset,
    OutputSpec,
    OverSpec,
    ParameterSpec,
    PhysicalSource,
    TimeSpec,
)
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import (
    assert_expr_calls,
    assert_expr_param_usage,
    compile_sql_expr,
    expression_columns,
    is_simple_ident,
    match_name,
    norm_name,
    parse_expression,
    require_ident,
)
from kpi_engine.pipeline.fn_apply import (
    COLUMN_FNS,
    WHERE_OPS,
    WHERE_OPS_HELP,
    column_op_error,
)
from kpi_engine.pipeline.compose import parse_compose_block
from kpi_engine.pipeline.filter_ops import FILTER_ARITY, assert_filter_arity, canonicalize_op
from kpi_engine.pipeline.loader import ensure_loaded
from kpi_engine.pipeline.op_protocol import CommonMeasureFields
from kpi_engine.pipeline.op_registry import get_op, require_op
from kpi_engine.runlog import traced

ensure_loaded()


def _require_kpis_dir(root: Path, *, source: str) -> Path:
    """YAML root must be a directory that contains kpis/."""
    if root.is_file():
        raise BindError(f"{source} is a file; set it to the folder that contains kpis/.")
    if not root.is_dir():
        raise BindError(
            f"{source} is not a directory. Set KPI_ENGINE_CONFIG_DIR or pass "
            "config_dir= to the folder that contains kpis/ and models/."
        )
    if not (root / "kpis").is_dir():
        raise BindError(
            f"No kpis/ directory under {root}. Set KPI_ENGINE_CONFIG_DIR or pass "
            "config_dir= to the kpi_config folder (the one that contains kpis/)."
        )
    return root


def default_config_dir() -> Path:
    """config_dir= wins at the caller. Else KPI_ENGINE_CONFIG_DIR, else sibling kpi_config/."""
    env = os.environ.get("KPI_ENGINE_CONFIG_DIR", "").strip()
    if env:
        return _require_kpis_dir(
            Path(env).expanduser().resolve(),
            source=f"KPI_ENGINE_CONFIG_DIR={env}",
        )
    pkg = Path(__file__).resolve().parents[1]
    sibling = (pkg.parent / "kpi_config").resolve()
    return _require_kpis_dir(sibling, source=str(sibling))


def _resolve_yaml(root: Path, kind: str, file_id: str, *, fold: bool) -> Path:
    """Return the unique YAML under kpis/ or models/ (flat or one group folder).

    KPIs match the filename stem exactly. Models also fold case / spaces /
    underscores (Sotif.yaml ↔ sotif). Two files for the same id is a bind error.
    """
    hits = _yaml_candidates(root, kind, file_id, fold=fold)
    flat = root / kind / f"{file_id}.yaml"
    nested = root / kind / "*" / f"{file_id}.yaml"
    if not hits:
        if kind == "kpis":
            raise BindError(f"No KPI YAML for kpi_id={file_id} at {flat} or {nested}.")
        raise BindError(f"No model YAML for model_id={file_id!r} at {flat} or {nested}.")
    if len(hits) > 1:
        listed = ", ".join(str(path) for path in sorted(hits))
        label = "kpi_id" if kind == "kpis" else "model_id"
        raise BindError(f"Multiple YAML files for {label}={file_id!r}: {listed}.")
    return hits[0]


def _yaml_candidates(root: Path, kind: str, file_id: str, *, fold: bool) -> list[Path]:
    """Flat <id>.yaml plus one-level <group>/<id>.yaml (not nested further)."""
    folder = root / kind
    wanted_name = f"{file_id}.yaml"
    wanted_fold = norm_name(str(file_id))
    hits: list[Path] = []
    seen: set[Path] = set()

    def consider(path: Path) -> None:
        if not path.is_file():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        if fold:
            if norm_name(path.stem) != wanted_fold:
                return
        elif path.name != wanted_name:
            return
        seen.add(resolved)
        hits.append(path)

    if not folder.is_dir():
        consider(folder / wanted_name)
        return hits

    for candidate in folder.glob("*.yaml"):
        consider(candidate)
    for group in folder.iterdir():
        if not group.is_dir() or group.name.startswith("."):
            continue
        for candidate in group.glob("*.yaml"):
            consider(candidate)
    return hits


@traced
def load_kpi(
    kpi_id: int | str,
    config_dir: Path | None = None,
    parameters: dict[str, Any] | None = None,
    *,
    selected_dimensions: tuple[str, ...] | Mapping[str, bool] | None = None,
    _validate_cases: bool = True,
) -> KpiSpec:
    """Bind request parameters, resolve when:/from_param:, then parse.

    ``parameters=None`` means ``{}`` (defaults fill). There is no template path
    vs 3004 path — 3004 is identity resolve. ``selected_dimensions=None`` means
    the key was omitted (use YAML default_dimensions).
    """
    from dataclasses import replace as _replace

    from kpi_engine.pipeline.parameters import apply_bound_to_spec, bind_incoming
    from kpi_engine.pipeline.resolve import resolve_kpi, validate_when_cases, yaml_has_overlays

    root = config_dir or default_config_dir()
    path = _resolve_yaml(root, "kpis", str(kpi_id), fold=False)
    raw = _read_yaml(path)
    schema = _parse_parameters(raw.get("parameters"))
    time = _parse_time(raw.get("time"))
    bound = bind_incoming(
        parameters if parameters is not None else {},
        schema,
        time=time,
        cut_names=_cut_names(raw),
        measure_keys=tuple(str(k) for k in (raw.get("measures") or {})),
        kpi_id=kpi_id,
    )
    materialized, model_templated = resolve_kpi(raw, bound)
    kpi = _parse_kpi(materialized, expected_id=kpi_id)
    bound = _replace(bound, model_templated=model_templated)
    kpi = apply_bound_to_spec(kpi, bound)
    kpi = apply_request_grain(kpi, selected_dimensions)
    models = _load_kpi_models(kpi, root)
    assert_default_grain_on_schema(kpi, models)
    if model_templated or yaml_has_overlays(raw):
        assert_pack_columns(kpi, models)
    if _validate_cases:
        validate_when_cases(
            raw,
            bound,
            parse_kpi=lambda data: _parse_kpi(data, expected_id=kpi_id),
            load_model=lambda mid: load_model(mid, root),
            check_columns=assert_pack_columns,
        )
    return kpi


def apply_request_grain(
    kpi: KpiSpec,
    selected: tuple[str, ...] | Mapping[str, bool] | None,
) -> KpiSpec:
    """Set request_grain from omitted/empty/names/bool-map selected_dimensions."""
    catalog = {norm_name(spec.name): spec.name for spec in kpi.dimension_specs}
    time_col = kpi.time.column if kpi.time is not None else None

    def canonical(name: str) -> str:
        raw = str(name).strip()
        if not raw:
            raise BindError("selected_dimensions names cannot be empty.")
        key = norm_name(raw)
        if time_col is not None and key == norm_name(time_col):
            raise BindError(
                f"selected_dimensions cannot include the time column {time_col!r}."
            )
        mapped = catalog.get(key)
        if mapped is None:
            raise BindError(
                f"Unknown selected_dimensions name {name!r}. "
                f"Catalog: {list(kpi.dimensions)}."
            )
        return mapped

    if selected is None:
        grain = kpi.default_dimensions
    elif isinstance(selected, Mapping):
        seen: set[str] = set()
        grain_list: list[str] = []
        for spec in kpi.dimension_specs:
            flag = None
            for key, value in selected.items():
                if norm_name(str(key)) == norm_name(spec.name):
                    flag = value
                    break
            if flag is True and spec.name not in seen:
                grain_list.append(spec.name)
                seen.add(spec.name)
        extra = [
            str(key)
            for key in selected
            if norm_name(str(key)) not in catalog
        ]
        if extra:
            raise BindError(
                f"Unknown selected_dimensions name {extra[0]!r}. "
                f"Catalog: {list(kpi.dimensions)}."
            )
        grain = tuple(grain_list)
    else:
        names: list[str] = []
        seen_names: set[str] = set()
        for item in selected:
            mapped = canonical(item)
            if mapped in seen_names:
                continue
            names.append(mapped)
            seen_names.add(mapped)
        grain = tuple(names)
    return replace(kpi, request_grain=grain)


def _cut_names(raw: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for cut in raw.get("cuts") or []:
        if isinstance(cut, dict) and cut.get("name"):
            names.append(str(cut["name"]))
    return tuple(names)


def _load_kpi_models(kpi: KpiSpec, root: Path) -> dict[str, ModelSpec]:
    models: dict[str, ModelSpec] = {}
    ids: list[str] = []
    seen: set[str] = set()
    for measure in kpi.base_measures:
        mid = measure.model_id or kpi.model_id
        key = norm_name(mid)
        if key in seen:
            continue
        seen.add(key)
        ids.append(mid)
    if not ids and kpi.model_id:
        ids.append(kpi.model_id)
    for mid in ids:
        try:
            model = load_model(mid, root)
        except BindError:
            continue
        models[norm_name(mid)] = model
        models[mid] = model
    return models


def assert_default_grain_on_schema(
    kpi: KpiSpec, models: Mapping[str, ModelSpec]
) -> None:
    """If the primary model lists output_schema, defaults and default-cut extras must resolve."""
    model = models.get(norm_name(kpi.model_id)) or models.get(kpi.model_id)
    if model is None or not model.output_schema:
        return
    rename = {spec.name: spec.source or spec.name for spec in kpi.dimension_specs}
    default_cut = next((c for c in kpi.cuts if c.name == kpi.default_cut), None)
    extras = default_cut.group_by if default_cut is not None else ()
    for name in (*kpi.default_dimensions, *extras):
        physical = rename.get(name, name)
        if (
            match_name(physical, model.output_schema) is None
            and match_name(name, model.output_schema) is None
        ):
            raise BindError(
                f"default_dimensions/cut extra {name!r} (column {physical!r}) "
                f"is not on model {model.model_id!r} output_schema."
            )


def assert_pack_columns(kpi: KpiSpec, models: Mapping[str, ModelSpec]) -> None:
    """Bind-error when a remaining base/expr column is missing from the chosen model."""
    for base in kpi.base_measures:
        mid = base.model_id or kpi.model_id
        model = models.get(norm_name(mid)) or models.get(mid)
        if model is None:
            continue
        schema = set(model.output_schema)
        if not schema:
            continue
        needed: list[str] = list(base.columns)
        if not needed and is_simple_ident(base.sql):
            needed = [base.sql]
        for col in needed:
            if match_name(col, schema) is None:
                raise BindError(
                    f"Model {model.model_id!r} is missing column {col!r} needed by "
                    f"base_measures.{base.name}. when: that measure or pick a model "
                    f"that has it."
                )


@traced
def load_model(model_id: str, config_dir: Path | None = None) -> ModelSpec:
    """Load kpi_config/models/<model_id>.yaml or models/<group>/<model_id>.yaml (name fold)."""
    root = config_dir or default_config_dir()
    path = _resolve_yaml(root, "models", str(model_id), fold=True)
    return _parse_model(_read_yaml(path), expected_id=model_id)


@traced
def bind_datasets(
    model: ModelSpec, request: AdaptedRequest
) -> dict[str, DatasetBinding]:
    """Match model aliases to context datasets. Context path wins; YAML default fills gaps."""
    by_alias = {d.alias.lower(): d for d in request.datasets}
    by_key = {d.key.lower(): d for d in request.datasets}
    defaults = _path_defaults(model)
    bound: dict[str, DatasetBinding] = {}
    for alias in model.required_aliases:
        hit = by_alias.get(alias.lower()) or by_key.get(alias.lower())
        default_path, table_type = defaults.get(alias.lower(), (None, "PARQUET"))
        if hit is not None and hit.path:
            bound[alias] = hit
            continue
        if hit is not None and default_path:
            bound[alias] = replace(hit, path=default_path, table_type=hit.table_type or table_type)
            continue
        if default_path:
            bound[alias] = DatasetBinding(
                key=alias,
                alias=alias,
                path=default_path,
                table_type=table_type,
                columns=(),
                mappings=(),
            )
            continue
        available = sorted({*(d.alias for d in request.datasets), *(d.key for d in request.datasets)})
        raise BindError(
            f"Model {model.model_id!r} requires alias {alias!r} "
            f"(no context path and no model default_path). "
            f"context datasets have {available}."
        )
    return bound


def _path_defaults(model: ModelSpec) -> dict[str, tuple[str, str]]:
    """alias.lower() → (path, table_type) from default_paths and sources.default_path."""
    out: dict[str, tuple[str, str]] = {}
    for alias, path in model.default_paths.items():
        if path:
            out[str(alias).lower()] = (str(path), "PARQUET")
    for source in model.sources:
        if not source.default_path:
            continue
        value = (source.default_path, source.table_type or "PARQUET")
        out[source.alias.lower()] = value
        out[source.name.lower()] = value
    return out


def fold_measure_keys(kpi: KpiSpec, requested: tuple[str, ...]) -> tuple[str, ...]:
    """Map host measure_key spellings onto KPI YAML keys (Previous_Year_Value)."""
    known = [m.key for m in kpi.measures]
    out: list[str] = []
    for key in requested:
        hit = match_name(key, known)
        if hit is None:
            compact = norm_name(key).replace("_", "")
            hit = next((k for k in known if norm_name(k).replace("_", "") == compact), None)
        out.append(hit or key)
    return tuple(dict.fromkeys(out))


@traced
def assert_measure_keys(kpi: KpiSpec, requested: tuple[str, ...]) -> None:
    """Fail if the context asked for a measure_key not declared in KPI YAML."""
    folded = fold_measure_keys(kpi, requested)
    known = {m.key for m in kpi.measures}
    unknown = [k for k in folded if k not in known]
    if unknown:
        raise BindError(
            f"Unknown measure_key(s) {unknown}. Valid keys: {sorted(known)}."
        )


@traced
def resolve_requested_graph(
    kpi: KpiSpec, requested: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[BaseMeasure, ...]]:
    """Requested keys (no catalog fallback) and the base_measures that graph needs.

    Walks arithmetic / fn / expr / rank / percent_of_total `of` and each measure's `of:` base.
    An empty measures_required list computes nothing — it does not expand to
    every key in the KPI YAML.
    """
    keys = list(requested)
    if requested and kpi.green_when is not None:
        keys.append(kpi.green_when.of)
    if requested and kpi.having is not None:
        from kpi_engine.pipeline.predicates import predicate_names

        keys.extend(predicate_names(kpi.having.predicates))
    emit = tuple(dict.fromkeys(fold_measure_keys(kpi, tuple(keys))))
    by_key = {m.key: m for m in kpi.measures}
    by_base = {b.name: b for b in kpi.base_measures}
    needed: set[str] = set()
    seen: set[str] = set()

    def walk_base(name: str) -> None:
        if name not in by_base or name in needed:
            return
        needed.add(name)
        from kpi_engine.pipeline.row_pipeline import base_dep_names

        for dep in base_dep_names(by_base[name], by_base):
            walk_base(dep)

    def walk(name: str) -> None:
        """Collect this key's bases and the measures it depends on."""
        if name in seen:
            return
        seen.add(name)
        if name in by_base:
            walk_base(name)
        spec = by_key.get(name)
        if spec is None:
            return
        if spec.of:
            walk(spec.of)
        for dep in measure_dependencies(spec):
            walk(dep)

    for key in emit:
        walk(key)
    bases = tuple(base for base in kpi.base_measures if base.name in needed)
    return emit, bases


def same_model_id(left: str | None, right: str | None) -> bool:
    """True when two model ids match after folding case / spaces / underscores."""
    if not left or not right:
        return False
    return norm_name(left) == norm_name(right)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML object from disk using safe_load (no arbitrary Python)."""
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise BindError(f"{path} must contain a YAML object.")
    return raw


def _parse_kpi(raw: dict[str, Any], expected_id: int | str) -> KpiSpec:
    """Turn a KPI YAML dict into KpiSpec."""
    kpi_id = raw.get("kpi_id", expected_id)
    time = _parse_time(raw.get("time"))
    parameter_schema = _parse_parameters(raw.get("parameters"))
    param_names = frozenset(spec.name for spec in parameter_schema)
    param_types = {spec.name: spec.type_name for spec in parameter_schema}

    dim_specs = tuple(_parse_dimension(d) for d in raw.get("dimensions") or [])
    dimensions = tuple(d.name for d in dim_specs)
    _assert_dimension_catalog(dim_specs, time)

    bases: list[BaseMeasure] = []
    for name, spec in (raw.get("base_measures") or {}).items():
        bases.append(_parse_base_measure(name, spec, param_names, param_types))
    _assert_base_pipeline(tuple(bases), default_model=str(raw.get("model") or "") or None)

    cuts = tuple(_parse_cut(c, dimensions) for c in raw.get("cuts") or [])
    if not cuts:
        raise BindError("At least one cut is required.")
    default_cut = str(raw.get("default_cut") or cuts[0].name)
    if default_cut not in {c.name for c in cuts}:
        raise BindError(f"default_cut {default_cut!r} is not a declared cut.")

    default_dimensions = _parse_default_dimensions(raw, dim_specs)
    identity_grain = _parse_identity_grain(raw, dim_specs)
    _assert_cut_grain_rules(cuts, default_dimensions, dim_specs)
    filter_map = {
        str(k): require_ident(str(v), what="filter_map column")
        for k, v in (raw.get("filter_map") or {}).items()
    }
    filter_specs = _parse_filters(raw.get("filters"))
    _assert_ignore_exclude_coupling(cuts, dim_specs, filter_specs, filter_map)

    measures = tuple(
        _parse_measure(k, v, param_names, param_types) for k, v in (raw.get("measures") or {}).items()
    )
    if not measures:
        raise BindError("measures cannot be empty.")
    clash = sorted(param_names & {m.key for m in measures})
    if clash:
        raise BindError(
            f"Parameter name(s) {clash} collide with measure keys. "
            "Rename the parameter."
        )
    _assert_measure_graph(measures, tuple(bases), dimensions)
    if time is None:
        _assert_snapshot_measures(measures)

    _assert_filter_specs(filter_specs, cuts, dimensions)
    row_set = raw.get("row_set", "span_union")
    if row_set not in {"span_union", "anchor_only"}:
        raise BindError("row_set must be span_union or anchor_only.")

    data_points = _parse_data_points(raw.get("data_points"), time)
    meta = _parse_meta(raw.get("meta"), tuple(m.key for m in measures))
    green_when = _parse_green_when(raw.get("green_when"), tuple(m.key for m in measures))
    having = _parse_having(raw.get("having"), measures, tuple(d.name for d in dim_specs))
    relations = tuple(
        _parse_relation(r, time, dimensions) for r in raw.get("model_relations") or []
    )
    base_names = {b.name for b in bases}
    for rel in relations:
        if rel.left not in base_names or rel.right not in base_names:
            raise BindError(
                f"model_relations left/right must be base_measures names "
                f"(got {rel.left!r}, {rel.right!r}; known {sorted(base_names)})."
            )
    kpi = KpiSpec(
        kpi_id=kpi_id,
        version=int(raw.get("version") or 1),
        model_id=str(raw.get("model")),
        time=time,
        dimensions=dimensions,
        base_measures=tuple(bases),
        cuts=cuts,
        default_cut=default_cut,
        measures=measures,
        filter_map=filter_map,
        filter_specs=filter_specs,
        row_set=row_set,  # type: ignore[arg-type]
        model_relations=relations,
        dimension_specs=dim_specs,
        data_points=data_points,
        meta=meta,
        green_when=green_when,
        parameter_schema=parameter_schema,
        default_dimensions=default_dimensions,
        request_grain=default_dimensions,
        identity_grain=identity_grain,
        having=having,
    )
    for spec in kpi.measures:
        if spec.trailing_from == "data_points" and data_points is None:
            raise BindError(
                f"measures.{spec.key} trailing.from: data_points needs a top-level data_points:."
            )
        get_op(spec.kind).validate(spec, kpi)
    from kpi_engine.capabilities.ops.support import assert_last_n_consumers

    assert_last_n_consumers(kpi)
    return kpi


def _parse_dimension(raw: Any) -> DimensionSpec:
    """Parse a dimension name or {name, from, map, default, grain}."""
    if isinstance(raw, str):
        name = require_ident(raw, what="dimension")
        return DimensionSpec(name=name, source=name)
    if not isinstance(raw, dict) or not raw.get("name"):
        raise BindError("Each dimension needs a name.")
    if raw.get("model") is not None:
        raise BindError(
            "dimensions cannot set model:; dimensions live on the KPI, not the extract."
        )
    name = require_ident(str(raw["name"]), what="dimension")
    source = require_ident(str(raw.get("from") or name), what="dimension.from")
    mapping_raw = raw.get("map") or {}
    if mapping_raw and not isinstance(mapping_raw, dict):
        raise BindError(f"dimensions.{name}.map must be an object.")
    mapping = {str(k): str(v) for k, v in mapping_raw.items()}
    grain = raw.get("grain")
    if grain is not None and grain not in GRAIN_NAMES:
        raise BindError(
            f"dimensions.{name}.grain must be day, week, month, quarter, or year."
        )
    default = raw.get("default")
    cardinality = raw.get("cardinality")
    if cardinality is not None and str(cardinality) != "high":
        raise BindError(
            f"dimensions.{name}.cardinality must be high when set."
        )
    return DimensionSpec(
        name=name,
        source=source,
        mapping=mapping,
        default=None if default is None else str(default),
        grain=grain,
        cardinality=str(cardinality) if cardinality is not None else None,
    )


def _assert_dimension_catalog(
    specs: tuple[DimensionSpec, ...], time: TimeSpec | None
) -> None:
    """Unique names/from:, and no clash with the time column."""
    seen_names: set[str] = set()
    seen_from: set[str] = set()
    time_key = norm_name(time.column) if time is not None else None
    for spec in specs:
        name_key = norm_name(spec.name)
        from_key = norm_name(spec.source)
        if name_key in seen_names:
            raise BindError(f"Duplicate dimension name {spec.name!r}.")
        seen_names.add(name_key)
        if from_key in seen_from:
            raise BindError(
                f"dimensions.{spec.name}.from {spec.source!r} collides with another "
                "dimension physical column."
            )
        seen_from.add(from_key)
        if time_key is not None and (name_key == time_key or from_key == time_key):
            raise BindError(
                f"dimensions.{spec.name} cannot use the time column {time.column!r}."
            )


def _parse_default_dimensions(
    raw: dict[str, Any], specs: tuple[DimensionSpec, ...]
) -> tuple[str, ...]:
    """Require default_dimensions: as a list of catalog names (empty is legal)."""
    if "default_dimensions" not in raw:
        raise BindError("default_dimensions is required.")
    value = raw.get("default_dimensions")
    if not isinstance(value, list):
        raise BindError("default_dimensions must be a list.")
    catalog = {norm_name(spec.name): spec.name for spec in specs}
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        ident = require_ident(str(item), what="default_dimensions")
        mapped = catalog.get(norm_name(ident))
        if mapped is None:
            raise BindError(
                f"default_dimensions {ident!r} is not a catalog dimension "
                f"(have {list(catalog.values())})."
            )
        if mapped in seen:
            continue
        names.append(mapped)
        seen.add(mapped)
    return tuple(names)


def _parse_identity_grain(
    raw: dict[str, Any], specs: tuple[DimensionSpec, ...]
) -> tuple[str, ...]:
    """Optional identity_grain: subset of dimensions; empty/omitted means unset."""
    if "identity_grain" not in raw:
        return ()
    value = raw.get("identity_grain")
    if not isinstance(value, list):
        raise BindError("identity_grain must be a list.")
    catalog = {norm_name(spec.name): spec.name for spec in specs}
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        ident = require_ident(str(item), what="identity_grain")
        mapped = catalog.get(norm_name(ident))
        if mapped is None:
            raise BindError(
                f"identity_grain {ident!r} is not a catalog dimension "
                f"(have {list(catalog.values())})."
            )
        if mapped in seen:
            continue
        names.append(mapped)
        seen.add(mapped)
    return tuple(names)


def _assert_cut_grain_rules(
    cuts: tuple[CutSpec, ...],
    default_dimensions: tuple[str, ...],
    specs: tuple[DimensionSpec, ...],
) -> None:
    """group_by is extras only: disjoint from defaults and from exclude_from_grain."""
    defaults = {norm_name(name) for name in default_dimensions}
    catalog = {norm_name(spec.name): spec.name for spec in specs}
    for cut in cuts:
        overlap = [name for name in cut.group_by if norm_name(name) in defaults]
        if overlap:
            raise BindError(
                f"cuts.{cut.name}.group_by {overlap[0]!r} is already in "
                "default_dimensions. group_by lists extras only; move the name "
                "to default_dimensions."
            )
        exclude = {norm_name(name) for name in cut.exclude_from_grain}
        both = [name for name in cut.group_by if norm_name(name) in exclude]
        if both:
            raise BindError(
                f"cuts.{cut.name} cannot list {both[0]!r} in both group_by and "
                "exclude_from_grain."
            )
        for name in (*cut.group_by, *cut.exclude_from_grain):
            if catalog and norm_name(name) not in catalog:
                raise BindError(
                    f"cuts.{cut.name} names {name!r} which is not a catalog dimension."
                )


def _assert_ignore_exclude_coupling(
    cuts: tuple[CutSpec, ...],
    specs: tuple[DimensionSpec, ...],
    filter_specs: tuple[FilterApplySpec, ...],
    filter_map: dict[str, str],
) -> None:
    """Dim-named ignore_filters must match exclude_from_grain both ways."""
    for cut in cuts:
        exclude = {norm_name(name) for name in cut.exclude_from_grain}
        ignore = {norm_name(name) for name in cut.ignore_filters}
        for token in cut.ignore_filters:
            dim = _dim_matched_by_ignore(token, specs, filter_specs, filter_map)
            if dim is None:
                continue
            if norm_name(dim.name) not in exclude:
                raise BindError(
                    f"cuts.{cut.name}.ignore_filters {token!r} matches dimension "
                    f"{dim.name!r}; add it to exclude_from_grain."
                )
        for name in cut.exclude_from_grain:
            dim = next(
                (spec for spec in specs if norm_name(spec.name) == norm_name(name)),
                None,
            )
            if dim is None:
                continue
            if norm_name(dim.name) not in ignore:
                raise BindError(
                    f"cuts.{cut.name}.exclude_from_grain {name!r} requires "
                    f"ignore_filters to list {dim.name!r}."
                )
            for spec in filter_specs:
                if not _filter_targets_dim(spec.code, spec.column, dim):
                    continue
                if norm_name(spec.code) not in ignore:
                    raise BindError(
                        f"cuts.{cut.name}.exclude_from_grain {name!r} requires "
                        f"ignore_filters to list YAML filter {spec.code!r}."
                    )
            for code, column in filter_map.items():
                if not _filter_targets_dim(code, column, dim):
                    continue
                if norm_name(code) not in ignore:
                    raise BindError(
                        f"cuts.{cut.name}.exclude_from_grain {name!r} requires "
                        f"ignore_filters to list {code!r}."
                    )


def _dim_matched_by_ignore(
    token: str,
    specs: tuple[DimensionSpec, ...],
    filter_specs: tuple[FilterApplySpec, ...],
    filter_map: dict[str, str],
) -> DimensionSpec | None:
    """Catalog dim D if this ignore token folds to D's name, from, filter, or map."""
    key = norm_name(token)
    for spec in specs:
        if key in {norm_name(spec.name), norm_name(spec.source)}:
            return spec
    for filt in filter_specs:
        if key in {norm_name(filt.code), norm_name(filt.column)}:
            for spec in specs:
                if _filter_targets_dim(filt.code, filt.column, spec):
                    return spec
    for code, column in filter_map.items():
        if key in {norm_name(code), norm_name(column)}:
            for spec in specs:
                if norm_name(column) in {norm_name(spec.name), norm_name(spec.source)}:
                    return spec
    return None


def _filter_targets_dim(code: str, column: str, dim: DimensionSpec) -> bool:
    """True when a filter code/column folds onto this dimension."""
    keys = {norm_name(code), norm_name(column)}
    return bool(keys & {norm_name(dim.name), norm_name(dim.source)})


def _parse_base_measure(
    name: str,
    spec: Any,
    param_names: frozenset[str] = frozenset(),
    param_types: Mapping[str, str] | None = None,
) -> BaseMeasure:
    """Parse sql:/columns:/op:/expr:/lookup:/over:/agg:/where for one base fact."""
    if not isinstance(spec, dict):
        raise BindError(f"base_measures.{name} must be an object.")
    lookup = _parse_lookup(name, spec.get("lookup"))
    over = _parse_over(name, spec.get("over"))
    replace_col = bool(spec.get("replace"))
    agg_ok = bool(spec.get("agg_ok"))
    columns, column_params = _parse_column_args(name, spec.get("columns"))
    row_op = spec.get("op") or spec.get("row_op")
    expr_raw = spec.get("expr")
    expr = str(expr_raw).strip() if expr_raw else None
    formula_kinds = sum(
        bool(x) for x in (expr, lookup, over, spec.get("sql"), row_op or columns)
    )
    if lookup and formula_kinds > 1:
        raise BindError(
            f"base_measures.{name} lookup: cannot combine with expr:/over:/sql:/columns:."
        )
    if over and (expr or spec.get("sql") or row_op or columns):
        raise BindError(
            f"base_measures.{name} over: cannot combine with expr:/sql:/columns:/op:."
        )
    if expr:
        node = parse_expression(expr, what=f"base_measures.{name}.expr")
        assert_expr_calls(node, COLUMN_FNS, what=f"base_measures.{name}.expr")
        assert_expr_param_usage(node, param_types or {}, what=f"base_measures.{name}.expr")
        if row_op is not None or columns:
            raise BindError(
                f"base_measures.{name} uses `expr:` and cannot also set `columns:` / `op:`."
            )
        if spec.get("sql"):
            raise BindError(
                f"base_measures.{name} uses `expr:` and cannot also set `sql:`."
            )
        columns = tuple(
            col
            for col in expression_columns(
                parse_expression(expr, what=f"base_measures.{name}.expr")
            )
            if col not in param_names
        )
    if row_op is not None:
        row_op = str(row_op)
        if row_op not in COLUMN_FNS:
            raise BindError(
                f"base_measures.{name} unknown op {row_op!r}. Registered: "
                f"{sorted(COLUMN_FNS)}. Add it under capabilities/functions/ "
                "and registries/functions/column.yaml."
            )
    elif column_params:
        raise BindError(
            f"base_measures.{name} names its columns but has no `op:` to bind them to."
        )
    sql_raw = str(spec.get("sql") or "").strip()
    if lookup is not None:
        sql_raw = lookup.column
        columns = (lookup.column,)
    if over is not None:
        cols = list(over.partition_by) + list(over.order_by)
        if over.of:
            cols.append(over.of)
        columns = tuple(dict.fromkeys(cols))
        sql_raw = sql_raw or (columns[0] if columns else name)
    if not sql_raw and columns:
        sql_raw = columns[0]
    if not sql_raw and expr:
        sql_raw = columns[0] if columns else ""
    if not sql_raw:
        raise BindError(
            f"base_measures.{name} needs sql:, columns:, expr:, lookup:, or over:."
        )
    if sql_raw and lookup is None and over is None:
        compile_sql_expr(sql_raw, what="measure sql")
    if row_op is not None:
        problem = column_op_error(row_op, len(columns) or 1, column_params)
        if problem:
            raise BindError(f"base_measures.{name} op {problem}")
    if not expr and sql_raw and not is_simple_ident(sql_raw) and row_op is None and lookup is None and over is None:
        expr = sql_raw
        sql_node = parse_expression(expr, what="measure sql")
        assert_expr_calls(sql_node, COLUMN_FNS, what=f"base_measures.{name}.sql")
        assert_expr_param_usage(sql_node, param_types or {}, what=f"base_measures.{name}.sql")
        columns = tuple(col for col in expression_columns(sql_node) if col not in param_names)
    helper_shape = lookup is not None or over is not None or bool(expr)
    if "agg" not in spec:
        agg = None if helper_shape else "sum"
        if row_op is not None and not helper_shape:
            agg = spec.get("agg") or "sum"
    else:
        agg = spec.get("agg")
    allowed = {
        "sum", "avg", "count", "min", "max", "count_distinct", "median", "percentile", "first", "last"
    }
    if agg is not None and agg not in allowed:
        raise BindError(f"Unknown agg {agg!r} on {name}.")
    percentile = spec.get("percentile")
    pvalue = None
    if percentile is not None:
        pvalue = float(percentile)
        if pvalue > 1:
            pvalue = pvalue / 100.0
        if pvalue < 0 or pvalue > 1:
            raise BindError(f"base_measures.{name}.percentile must be in 0-1 or 0-100.")
    if agg == "percentile" and pvalue is None:
        raise BindError(f"base_measures.{name} agg=percentile requires percentile:.")
    where = _parse_where(name, spec.get("where"))
    model_id = spec.get("model")
    measure = BaseMeasure(
        name=require_ident(str(name), what="base measure"),
        sql=sql_raw,
        agg=agg,
        model_id=str(model_id) if model_id else None,
        percentile=pvalue,
        columns=columns,
        column_params=column_params,
        row_op=row_op,
        where=where,
        expr=expr,
        lookup=lookup,
        over=over,
        replace=replace_col,
        agg_ok=agg_ok,
    )
    from kpi_engine.pipeline.row_pipeline import assert_window_agg

    assert_window_agg(measure)
    return measure


def _parse_lookup(name: str, raw: Any) -> LookupSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError(f"base_measures.{name}.lookup must be an object.")
    column = require_ident(str(raw.get("column") or ""), what="lookup.column")
    mapping_raw = raw.get("map")
    if not isinstance(mapping_raw, dict) or not mapping_raw:
        raise BindError(f"base_measures.{name}.lookup needs a non-empty map:.")
    mapping = {str(key).strip(): value for key, value in mapping_raw.items()}
    strict = bool(raw.get("strict"))
    default = raw.get("default")
    if strict and default is not None:
        raise BindError(
            f"base_measures.{name}.lookup cannot set both strict: true and default:."
        )
    return LookupSpec(column=column, mapping=mapping, default=default, strict=strict)


def _parse_over(name: str, raw: Any) -> OverSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError(f"base_measures.{name}.over must be an object.")
    fn = str(raw.get("fn") or "").strip().lower()
    if fn not in OVER_FNS:
        raise BindError(
            f"base_measures.{name}.over.fn must be one of {sorted(OVER_FNS)} (got {fn!r})."
        )
    partition_by = _parse_name_list(raw.get("partition_by") or [], f"base_measures.{name}.over.partition_by")
    order_by = _parse_name_list(raw.get("order_by"), f"base_measures.{name}.over.order_by")
    if not order_by:
        raise BindError(f"base_measures.{name}.over.order_by needs at least one column.")
    of = raw.get("of")
    of_name = str(of).strip() if of else None
    needs_of = fn in {"lag", "lead", "running_sum", "running_avg", "last_n", "rank", "dense_rank"}
    if needs_of and not of_name and fn not in {"rank", "dense_rank"}:
        raise BindError(
            f"base_measures.{name}.over.fn={fn} requires of: "
            "(it does not default to this helper's name)."
        )
    n_raw = raw.get("n")
    n = None
    if n_raw is not None:
        try:
            n = int(n_raw)
        except (TypeError, ValueError) as exc:
            raise BindError(f"base_measures.{name}.over.n must be an integer.") from exc
        if n < 1:
            raise BindError(f"base_measures.{name}.over.n must be >= 1.")
    if fn in {"lag", "lead", "last_n"} and n is None:
        n = 1
    return OverSpec(
        fn=fn,
        partition_by=partition_by,
        order_by=order_by,
        of=of_name,
        n=n,
    )


def _parse_name_list(raw: Any, what: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BindError(f"{what} must be a list.")
    return tuple(require_ident(str(item), what=what) for item in raw)


def _assert_base_pipeline(
    bases: tuple[BaseMeasure, ...], *, default_model: str | None
) -> None:
    """Topo-sort (cycle check) and reject over/lookup columns on another extract."""
    from kpi_engine.pipeline.row_pipeline import topo_bases

    topo_bases(bases)
    by_name = {measure.name: measure for measure in bases}
    for measure in bases:
        if (
            measure.over is not None
            and measure.over.fn == "last_n"
            and measure.over.of
        ):
            src = by_name.get(measure.over.of)
            if src is not None and src.over is not None and src.over.fn == "last_n":
                raise BindError(
                    f"base_measures.{measure.name} over.fn=last_n cannot take "
                    f"of={measure.over.of!r} which is also last_n."
                )
        own = measure.model_id or default_model
        if not own:
            continue
        names: list[str] = []
        if measure.lookup is not None:
            names.append(measure.lookup.column)
        if measure.over is not None:
            names.extend(measure.over.partition_by)
            names.extend(measure.over.order_by)
            if measure.over.of:
                names.append(measure.over.of)
        for name in names:
            other = by_name.get(name)
            if other is None:
                continue
            other_model = other.model_id or default_model
            if other_model and not same_model_id(own, other_model):
                kind = "over" if measure.over is not None else "lookup"
                raise BindError(
                    f"base_measures.{measure.name} {kind} column {name!r} is on "
                    f"model {other_model!r}, not this extract {own!r}."
                )


def _parse_having(
    raw: Any, measures: tuple[OutputSpec, ...], dimensions: tuple[str, ...]
) -> HavingSpec | None:
    if raw is None:
        return None
    from kpi_engine.pipeline.predicates import (
        assert_scalar_ofs,
        parse_match,
        parse_predicates,
        predicate_names,
    )

    if isinstance(raw, (list, tuple)):
        predicates = parse_predicates(raw, what="having")
        match = "all"
        then_group_by = None
    elif isinstance(raw, dict):
        predicates = parse_predicates(raw.get("predicates"), what="having")
        match = parse_match(raw.get("match"), what="having")
        if "then_group_by" in raw:
            then_raw = raw.get("then_group_by")
            if then_raw is None:
                then_group_by = ()
            else:
                then_group_by = _parse_name_list(then_raw, "having.then_group_by")
                unknown = [n for n in then_group_by if n not in dimensions]
                if unknown:
                    raise BindError(
                        f"having.then_group_by {unknown} is not in dimensions: "
                        f"{sorted(dimensions)}."
                    )
        else:
            then_group_by = None
    else:
        raise BindError("having must be a list of predicates or an object.")
    by_key = {m.key for m in measures}
    for name in predicate_names(predicates):
        if name not in by_key:
            raise BindError(
                f"having of={name!r} is not a measure key. Declared: {sorted(by_key)}."
            )
    assert_scalar_ofs(predicates, measures, what="having")
    return HavingSpec(predicates=predicates, match=match, then_group_by=then_group_by)


def _parse_column_args(name: str, raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read `columns:` as an ordered list or a {parameter: column} mapping.

    The mapping form binds by keyword, so an order-sensitive op such as
    `divide` cannot silently be handed its operands the wrong way round.
    """
    if raw is None:
        return (), ()
    if isinstance(raw, dict):
        params = tuple(str(key) for key in raw)
        columns = tuple(
            require_ident(str(value), what="base_measures.columns") for value in raw.values()
        )
        return columns, params
    if not isinstance(raw, (list, tuple)):
        raise BindError(
            f"base_measures.{name}.columns must be a list of columns or a "
            "{parameter: column} mapping."
        )
    return tuple(require_ident(str(c), what="base_measures.columns") for c in raw), ()


def _parse_where(name: str, raw: Any) -> MeasureWhere | None:
    """Parse where: { column, op, values } for a Pandas mask."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError(f"base_measures.{name}.where must be an object.")
    column = require_ident(str(raw.get("column") or ""), what="where.column")
    try:
        op = canonicalize_op(raw.get("op") or "in")
    except BindError:
        op = str(raw.get("op") or "in").strip().lower()
    if op not in WHERE_OPS:
        raise BindError(f"base_measures.{name}.where.op must be {WHERE_OPS_HELP}.")
    values_raw = raw.get("values")
    if values_raw is None and "value" in raw:
        if FILTER_ARITY.get(op) == 2:
            raise BindError(
                f"base_measures.{name}.where op={op} requires values: [lo, hi], not value:."
            )
        values_raw = [raw["value"]]
    if values_raw is None:
        raise BindError(f"base_measures.{name}.where needs values:.")
    if not isinstance(values_raw, (list, tuple)):
        values_raw = [values_raw]
    values = tuple(values_raw)
    assert_filter_arity(op, values, code=f"base_measures.{name}.where")
    return MeasureWhere(column=column, op=op, values=values)


def _parse_parameters(raw: Any) -> tuple[ParameterSpec, ...]:
    """Parse optional YAML parameters:. Missing means none."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise BindError("parameters must be an object.")
    from kpi_engine.pipeline.parameters import COLLECTION_TYPES, ITEM_TYPES, SCALAR_TYPES

    out: list[ParameterSpec] = []
    seen: set[str] = set()
    for name, spec in raw.items():
        ident = require_ident(str(name), what="parameter")
        if ident in seen:
            raise BindError(f"Duplicate parameter {ident!r}.")
        seen.add(ident)
        if ident == "selected_dimensions":
            raise BindError(
                "parameters.selected_dimensions is not allowed. Send "
                "context.selected_dimensions (the request grain overlay)."
            )
        if not isinstance(spec, dict):
            raise BindError(f"parameters.{ident} must be an object.")
        type_name = spec.get("type")
        if type_name not in SCALAR_TYPES | COLLECTION_TYPES:
            raise BindError(
                f"parameters.{ident}.type must be string, int, float, bool, list, or dict."
            )
        item_type = spec.get("item")
        if type_name in COLLECTION_TYPES:
            if item_type is None:
                raise BindError(
                    f"parameters.{ident} type {type_name} requires item: "
                    "(string, int, float, or bool)."
                )
            if item_type not in ITEM_TYPES:
                raise BindError(
                    f"parameters.{ident}.item must be string, int, float, or bool."
                )
        elif item_type is not None:
            raise BindError(
                f"parameters.{ident}.item is only valid on type list or dict."
            )
        allowed_raw = spec.get("allowed")
        allowed: tuple[Any, ...] | None = None
        if allowed_raw is not None:
            if not isinstance(allowed_raw, (list, tuple)) or not allowed_raw:
                raise BindError(
                    f"parameters.{ident}.allowed must be a non-empty list."
                )
            allowed = tuple(allowed_raw)
        value_map = spec.get("map") or {}
        if not isinstance(value_map, dict):
            raise BindError(f"parameters.{ident}.map must be an object.")
        out.append(
            ParameterSpec(
                name=ident,
                type_name=str(type_name),
                default=spec.get("default"),
                has_default="default" in spec,
                allowed=allowed,
                value_map=dict(value_map),
                item_type=str(item_type) if item_type is not None else None,
            )
        )
    return tuple(out)


def _parse_time(raw: Any) -> TimeSpec | None:
    """Parse YAML time:. Missing/empty means a snapshot KPI with no period column."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError("time must be an object.")
    if not raw:
        return None
    column = raw.get("column")
    if not column:
        raise BindError("time.column is required when time is declared.")
    filter_code = str(raw.get("filter_code") or "").strip()
    if not filter_code:
        raise BindError(
            "time.filter_code is required when time is declared. "
            "Set it to the context filter that carries the selected period, "
            "or omit the time: block if this KPI has no time column."
        )
    grain = raw.get("grain", "month")
    if grain not in GRAIN_NAMES:
        raise BindError(
            f"Unknown time.grain {grain!r}. Use day, week, month, quarter, or year."
        )
    source_grain = raw.get("source_grain")
    if source_grain is not None and source_grain not in GRAIN_NAMES:
        raise BindError(
            f"Unknown time.source_grain {source_grain!r}. "
            "Use day, week, month, quarter, or year."
        )
    raw_grains = raw.get("grains")
    grains: tuple[str, ...] = ()
    if raw_grains is not None:
        if not isinstance(raw_grains, (list, tuple)) or not raw_grains:
            raise BindError("time.grains must be a non-empty list of grains.")
        grains = tuple(str(g) for g in raw_grains)
        unknown = [g for g in grains if g not in GRAIN_NAMES]
        if unknown:
            raise BindError(
                f"Unknown time.grains {unknown}. Use day, week, month, quarter, or year."
            )
        if grain not in grains:
            raise BindError(f"time.grain {grain!r} must appear in time.grains {list(grains)}.")
    calendar = str(raw.get("calendar") or "gregorian")
    if calendar not in {"gregorian", "fiscal"}:
        raise BindError(f"Unknown time.calendar {calendar!r}. Use gregorian or fiscal.")
    if raw.get("timezone") is not None:
        raise BindError(
            "time.timezone is not supported; timestamps are bucketed as stored. "
            "Remove it, or convert the column in a kind: sql model."
        )
    raw_fiscal_start = raw.get("fiscal_start_month")
    fiscal_start = 4 if raw_fiscal_start is None else int(raw_fiscal_start)
    if fiscal_start < 1 or fiscal_start > 12:
        raise BindError("time.fiscal_start_month must be 1-12.")
    raw_format = raw.get("format")
    time_format = str(raw_format).strip() if raw_format else None
    if time_format == "":
        time_format = None
    if time_format:
        from kpi_engine.dates import resolve_time_format

        resolve_time_format(time_format)
    compose_template = parse_compose_block(raw.get("compose"), what="time.compose")
    return TimeSpec(
        column=require_ident(str(column), what="time.column"),
        grain=grain,  # type: ignore[arg-type]
        filter_code=filter_code,
        calendar=calendar,
        fiscal_start_month=fiscal_start,
        format=time_format,
        compose_template=compose_template,
        source_grain=source_grain,
        grains=grains,  # type: ignore[arg-type]
    )


def measure_dependencies(spec: OutputSpec) -> tuple[str, ...]:
    """Measure keys this measure consumes (plugin-declared)."""
    return get_op(spec.kind).dependencies(spec)


def _assert_measure_graph(
    measures: tuple[OutputSpec, ...],
    bases: tuple[BaseMeasure, ...],
    dimensions: tuple[str, ...],
) -> None:
    """Fail at bind time on unknown references and dependency cycles."""
    by_key = {m.key: m for m in measures}
    known_bases = {b.name for b in bases}
    helpers = {b.name for b in bases if b.agg is None}
    for spec in measures:
        for name in measure_dependencies(spec):
            if name in by_key:
                dep = by_key[name]
                if get_op(dep.kind).phase == "cut" and get_op(spec.kind).phase != "cut":
                    raise BindError(
                        f"measures.{spec.key} cannot use {name!r} (op={dep.kind}) as an "
                        "input. Rank and percent_of_total are assigned after every row "
                        "on the cut."
                    )
                continue
            if get_op(spec.kind).phase == "cut" and name in known_bases:
                continue
            if name in helpers:
                raise BindError(
                    f"measures.{spec.key} of={name!r} is a row helper (no agg:). "
                    "Use it in later base expr/over, or declare identity_grain and "
                    f"measures.{spec.key} op=point of that helper."
                )
            raise BindError(
                f"measures.{spec.key} references unknown measure {name!r}. "
                f"Valid keys: {sorted(by_key)}."
            )

    state: dict[str, int] = {}

    def walk(key: str, trail: list[str]) -> None:
        """Depth-first search that reports the cycle it closed."""
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            cycle = trail[trail.index(key):] + [key]
            raise BindError(
                f"measures dependency cycle: {' -> '.join(cycle)}. "
                "A measure cannot depend on itself, directly or indirectly."
            )
        state[key] = 1
        trail.append(key)
        for name in measure_dependencies(by_key[key]):
            if name in by_key:
                walk(name, trail)
        trail.pop()
        state[key] = 2

    for spec in measures:
        walk(spec.key, [])


def _assert_snapshot_measures(measures: tuple[OutputSpec, ...]) -> None:
    """Window/trend/offset ops need a time column; snapshot KPIs cannot declare them."""
    bad: list[str] = []
    for spec in measures:
        plugin = get_op(spec.kind)
        if plugin.needs_time(spec):
            bad.append(f"{spec.key} ({spec.kind})")
    if bad:
        raise BindError(
            "This KPI has no time: block, so measures cannot use windows, trends, "
            f"or period offsets ({', '.join(bad)}). Add time.column / time.filter_code, "
            "or keep only current-period point / dimension / arithmetic / "
            "percent_of_total measures."
        )


def _parse_filters(raw: Any) -> tuple[FilterApplySpec, ...]:
    """Parse KPI YAML `filters:` — column, op, optional, apply extract|calc|result."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise BindError("filters must be an object keyed by context filter code.")
    out: list[FilterApplySpec] = []
    for code, spec in raw.items():
        if isinstance(spec, str):
            out.append(
                FilterApplySpec(
                    code=str(code),
                    column=require_ident(spec, what="filters.column"),
                )
            )
            continue
        if not isinstance(spec, dict):
            raise BindError(f"filters.{code} must be a column name or an object.")
        column = require_ident(str(spec.get("column") or ""), what="filters.column")
        apply = str(spec.get("apply") or "extract").strip().lower()
        if apply not in {"extract", "calc", "result"}:
            raise BindError(
                f"filters.{code}.apply must be extract, calc, or result (got {spec.get('apply')!r})."
            )
        if "optional" in spec and spec.get("optional") is False:
            raise BindError(
                f"filters.{code}.optional: false is not supported. "
                "All row filters are optional; omit the key or send [] to skip."
            )
        out.append(
            FilterApplySpec(
                code=str(code),
                column=column,
                op=canonicalize_op(spec.get("op") or "in"),
                optional=bool(spec.get("optional", False)),
                apply=apply,  # type: ignore[arg-type]
                compose_template=parse_compose_block(
                    spec.get("compose"), what=f"filters.{code}.compose"
                ),
            )
        )
    return tuple(out)


def _assert_filter_specs(
    specs: tuple[FilterApplySpec, ...],
    cuts: tuple[CutSpec, ...],
    dimensions: tuple[str, ...],
) -> None:
    """result cannot sit in ignore_filters; result columns are dimensions.

    apply: extract plus ignore_filters is legal. split_filters promotes that
    filter to calc when an emitted cut ignores it (G worldwide / R extract).
    """
    ignored = {norm_name(name) for cut in cuts for name in cut.ignore_filters}
    for spec in specs:
        names = {norm_name(spec.code), norm_name(spec.column)}
        listed = sorted(names & ignored)
        if listed and spec.apply == "result":
            raise BindError(
                f"filters.{spec.code} apply: result cannot be listed in ignore_filters "
                f"({listed[0]})."
            )
        if spec.apply == "result" and match_name(spec.column, dimensions) is None:
            raise BindError(
                f"filters.{spec.code} apply: result must name a dimension column "
                f"(got {spec.column!r}; dimensions {list(dimensions)})."
            )


def _parse_cut(raw: Any, dimensions: tuple[str, ...] = ()) -> CutSpec:
    """Parse one cut: name, extra group_by, exclude_from_grain, ignore_filters, also_emit."""
    if not isinstance(raw, dict):
        raise BindError("Each cut must be an object.")
    if raw.get("model") is not None:
        raise BindError("cuts cannot set model:; cuts live on the KPI, not the extract.")
    name = str(raw.get("name") or "")
    if not name:
        raise BindError("Cut name is required.")
    catalog = {norm_name(item): item for item in dimensions}

    def _dim_list(field: str, what: str) -> tuple[str, ...]:
        values = []
        seen: set[str] = set()
        for item in raw.get(field) or []:
            ident = require_ident(str(item), what=what)
            key = norm_name(ident)
            mapped = catalog.get(key)
            if dimensions and mapped is None:
                raise BindError(
                    f"cuts.{name}.{field} {ident!r} is not a catalog dimension "
                    f"(have {list(dimensions)})."
                )
            canonical = mapped or ident
            if canonical in seen:
                continue
            values.append(canonical)
            seen.add(canonical)
        return tuple(values)

    group_by = _dim_list("group_by", "cut group_by")
    exclude = _dim_list("exclude_from_grain", "cut exclude_from_grain")
    ignore = tuple(str(x) for x in raw.get("ignore_filters") or [])
    also = tuple(str(x) for x in raw.get("also_emit") or [])
    return CutSpec(
        name=name,
        group_by=group_by,
        ignore_filters=ignore,
        also_emit=also,
        exclude_from_grain=exclude,
    )


def _parse_relation(
    raw: Any,
    time: TimeSpec | None = None,
    dimensions: tuple[str, ...] = (),
) -> ModelRelation:
    """Parse one post-aggregation join between two base measures' models."""
    if not isinstance(raw, dict):
        raise BindError("Each model_relations entry must be an object.")
    how = str(raw.get("how") or "outer").lower()
    if how in {"full", "full_outer"}:
        how = "outer"
    if how not in {"outer", "left", "right", "inner"}:
        raise BindError(f"model_relations.how {how!r} is not outer/left/right/inner.")
    on = tuple(require_ident(str(c), what="model_relations.on") for c in raw.get("on") or [])
    if not on:
        raise BindError("model_relations.on must list join keys (time and dimensions).")
    allowed = {norm_name(name): name for name in dimensions}
    if time is not None:
        allowed[norm_name(time.column)] = time.column
    if allowed:
        for name in on:
            if norm_name(name) not in allowed:
                raise BindError(
                    f"model_relations.on {name!r} must be the time column or a "
                    f"catalog dimension (have {sorted(allowed.values())})."
                )
    return ModelRelation(
        left=str(raw.get("left") or ""),
        right=str(raw.get("right") or ""),
        on=on,
        how=how,
    )


def _parse_measure(
    key: str,
    raw: Any,
    param_names: frozenset[str] = frozenset(),
    param_types: Mapping[str, str] | None = None,
) -> OutputSpec:
    """Parse one requestable measure via the registered OpPlugin."""
    if not isinstance(raw, dict):
        raise BindError(f"measures.{key} must be an object.")
    kind = raw.get("kind") or raw.get("op")
    hint = ""
    if kind in {"percent_of_cut_total", "percent_gt", "share_of_total"}:
        hint = " For share of all groups on a cut, use op: percent_of_total."
    try:
        plugin = require_op(str(kind), what=f"measures.{key}")
    except BindError as exc:
        raise BindError(f"{exc}{hint}") from exc
    offset = None
    if raw.get("offset"):
        off = raw["offset"]
        offset = Offset(
            months=int(off.get("months") or 0),
            years=int(off.get("years") or 0),
            days=int(off.get("days") or 0),
            quarters=int(off.get("quarters") or 0),
            weeks=int(off.get("weeks") or 0),
        )
    trailing = None
    trailing_unit = None
    trailing_from = None
    if raw.get("trailing"):
        trail = raw["trailing"]
        if not isinstance(trail, dict):
            raise BindError(f"measures.{key}.trailing must be an object.")
        if trail.get("from") is not None:
            trailing_from = str(trail["from"])
            if trailing_from != "data_points":
                raise BindError(
                    f"measures.{key}.trailing.from must be data_points (got {trailing_from!r})."
                )
        for unit in ("periods", "months", "weeks", "days", "quarters", "years"):
            if trail.get(unit) is not None:
                if trailing_from:
                    raise BindError(
                        f"measures.{key}.trailing cannot set both from: and {unit}:."
                    )
                trailing = int(trail[unit])
                trailing_unit = {
                    "periods": None,
                    "months": "month",
                    "weeks": "week",
                    "days": "day",
                    "quarters": "quarter",
                    "years": "year",
                }[unit]
                break
    window_range = raw.get("range")
    if window_range is not None:
        from kpi_engine.contracts import WINDOW_RANGE_NAMES

        window_range = str(window_range)
        if window_range not in WINDOW_RANGE_NAMES:
            raise BindError(
                f"measures.{key}.range must be trailing, leading, cumulative, "
                "ytd, mtd, qtd, wtd, full_month, full_quarter, or full_year."
            )
    of_raw = raw.get("of")
    of = None
    operands: tuple[str, ...] = ()
    if isinstance(of_raw, (list, tuple)):
        operands = tuple(str(x) for x in of_raw)
    elif of_raw is not None:
        of = str(of_raw)
    cuts = tuple(raw["cuts"]) if raw.get("cuts") is not None else None
    return plugin.parse(
        key,
        CommonMeasureFields(
            of=of,
            operands=operands,
            offset=offset,
            trailing_months=trailing,
            trailing_from=trailing_from,
            trailing_unit=trailing_unit,
            inclusive=bool(raw.get("inclusive", True)),
            cuts=cuts,
            window_range=window_range,
            raw=raw,
            parameter_names=param_names,
            parameter_types=dict(param_types or {}),
        ),
    )


def _parse_partition_by(key: str, kind: str, raw: dict[str, Any]) -> tuple[str, ...]:
    """Read partition_by: (preferred) or group_by: (rank alias) as dimension names."""
    if raw.get("partition_by") is not None:
        raw_group = raw.get("partition_by")
        what = "partition_by"
    else:
        raw_group = raw.get("group_by") or []
        what = "group_by"
    if not isinstance(raw_group, (list, tuple)):
        raise BindError(f"measures.{key} op={kind} {what} must be a list.")
    return tuple(require_ident(str(c), what=f"{kind} {what}") for c in raw_group)


def _parse_fn_inputs(key: str, raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read `inputs:` as an ordered list or a {parameter: measure} mapping."""
    if isinstance(raw, dict):
        return tuple(str(v) for v in raw.values()), tuple(str(k) for k in raw)
    if not isinstance(raw, (list, tuple)):
        raise BindError(
            f"measures.{key}.inputs must be a list of measure keys or a "
            "{parameter: measure} mapping."
        )
    return tuple(str(x) for x in raw), ()


def _parse_model(raw: dict[str, Any], expected_id: str) -> ModelSpec:
    """Parse a physical (sources/joins) or sql model YAML."""
    model_id = str(raw.get("model_id") or expected_id)
    kind = raw.get("kind") or "physical"
    if kind not in {"physical", "sql"}:
        raise BindError(f"Unknown model kind {kind!r}.")
    aliases = tuple(
        require_ident(str(a), what="alias") for a in raw.get("required_aliases") or []
    )
    sources: list[PhysicalSource] = []
    for name, spec in (raw.get("sources") or {}).items():
        spec = spec or {}
        alias = spec.get("alias") or name
        sources.append(
            PhysicalSource(
                name=require_ident(str(name), what="source"),
                alias=require_ident(str(alias), what="source alias"),
                default_path=_optional_path(spec.get("default_path") or spec.get("path")),
                table_type=str(spec.get("table_type") or "PARQUET"),
            )
        )
        if str(alias) not in aliases:
            aliases = aliases + (require_ident(str(alias), what="alias"),)
    if kind == "physical" and not aliases:
        raise BindError("Physical models need required_aliases or sources.")

    joins: list[JoinSpec] = []
    for join in raw.get("joins") or []:
        joins.append(
            JoinSpec(
                left=require_ident(str(join["left"]), what="join.left"),
                right=require_ident(str(join["right"]), what="join.right"),
                on=tuple(require_ident(str(c), what="join.on") for c in join.get("on") or []),
                join_type=str(join.get("type") or join.get("join_type") or "left"),
            )
        )

    schema = tuple(
        require_ident(str(c["name"] if isinstance(c, dict) else c), what="output column")
        for c in raw.get("output_schema") or []
    )
    sql = raw.get("sql")
    if kind == "sql" and not sql:
        raise BindError("SQL models require a sql: block.")
    default_paths = _parse_default_paths(raw.get("default_paths"))
    return ModelSpec(
        model_id=model_id,
        kind=kind,  # type: ignore[arg-type]
        required_aliases=aliases,
        sources=tuple(sources),
        joins=tuple(joins),
        sql=sql,
        output_schema=schema,
        default_paths=default_paths,
    )


def _parse_default_paths(raw: Any) -> dict[str, str]:
    """Parse model.default_paths: alias → filesystem/URI path."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BindError("default_paths must be an object of alias: path.")
    out: dict[str, str] = {}
    for alias, value in raw.items():
        path = value.get("path") if isinstance(value, dict) else value
        if not path:
            raise BindError(f"default_paths.{alias} needs a path.")
        out[str(alias)] = str(path)
    return out


def _parse_data_points(raw: Any, time: TimeSpec | None) -> int | dict[str, int] | None:
    """Scalar length, or one positive int per allowed grain."""
    if raw is None:
        return None
    grains = tuple(time.grains) if time is not None and time.grains else (
        (time.grain,) if time is not None else ()
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float, dict)):
        raise BindError("data_points must be a positive integer or a grain map.")
    if isinstance(raw, (int, float)):
        if len(grains) > 1:
            raise BindError(
                "data_points must be a map when time.grains lists more than one grain."
            )
        n = int(raw)
        if n < 1:
            raise BindError("data_points must be a positive integer.")
        return n
    out: dict[str, int] = {}
    for key, value in raw.items():
        if str(key) not in GRAIN_NAMES:
            raise BindError(
                f"data_points key {key!r} is not day, week, month, quarter, or year."
            )
        n = int(value)
        if n < 1:
            raise BindError(f"data_points.{key} must be a positive integer.")
        out[str(key)] = n
    if len(grains) > 1:
        missing = [g for g in grains if g not in out]
        if missing:
            raise BindError(
                f"data_points must list every time.grains entry (missing {missing})."
            )
    return out


def _parse_meta(raw: Any, measure_keys: tuple[str, ...]) -> KpiMeta | None:
    """Literal KPI / ParentKPI / IsChild / SelectedMetrics."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError("meta must be an object.")
    selected = raw.get("SelectedMetrics", raw.get("selected_metrics")) or []
    if not isinstance(selected, (list, tuple)):
        raise BindError("meta.SelectedMetrics must be a list of measure keys.")
    names = tuple(str(x) for x in selected)
    unknown = [n for n in names if n not in measure_keys]
    if unknown:
        raise BindError(f"meta.SelectedMetrics names unknown measure(s) {unknown}.")
    kpi_name = raw.get("KPI", raw.get("kpi"))
    parent = raw.get("ParentKPI", raw.get("parent_kpi"))
    is_child = raw.get("IsChild", raw.get("is_child"))
    return KpiMeta(
        kpi=None if kpi_name is None else str(kpi_name),
        parent_kpi=None if parent is None else str(parent),
        is_child=None if is_child is None else bool(is_child),
        selected_metrics=names,
    )


def _parse_green_when(raw: Any, measure_keys: tuple[str, ...]) -> GreenWhen | None:
    """Threshold: green when the named measure is >= above or <= below."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindError("green_when must be an object.")
    of = raw.get("of")
    if not of:
        raise BindError("green_when.of must name a measure.")
    of = str(of)
    if of not in measure_keys:
        raise BindError(
            f"green_when.of {of!r} is not a measure key. Declared: {sorted(measure_keys)}."
        )
    has_above = raw.get("above") is not None
    has_below = raw.get("below") is not None
    if has_above == has_below:
        raise BindError("green_when needs exactly one of above: or below:.")
    return GreenWhen(
        of=of,
        above=float(raw["above"]) if has_above else None,
        below=float(raw["below"]) if has_below else None,
    )


def _optional_path(value: Any) -> str | None:
    """Treat blank YAML paths as missing so context can still supply them."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
