"""Load KPI/model YAML and bind context datasets by alias.

What this file provides
    default_config_dir, load_kpi, load_model, bind_datasets, assert_measure_keys.
    Parsers for cuts, measures, physical/SQL models.

Where it is used
    orchestrator after adapt(). Tests load KPI 3004 via load_kpi.

Capabilities
    - Reads config/kpis/<kpi_id>.yaml and config/models/<model_id>.yaml.
    - Validates identifiers, aggs, default_cut, measure keys.
    - Binds model.required_aliases to context.datasets by alias, then key.
    - Context path wins; model default_path / default_paths fills a missing alias.
    - Unknown measure_key is a hard error listing valid YAML keys.

When to use
    Change parsing when YAML schema changes (new measure op, new cut field).
    To onboard a KPI, add a YAML file — do not edit this module.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from kpi_engine.contracts import (
    AdaptedRequest,
    BaseMeasure,
    CutSpec,
    DatasetBinding,
    JoinSpec,
    KpiSpec,
    ModelRelation,
    ModelSpec,
    Offset,
    OutputSpec,
    PhysicalSource,
    TimeSpec,
)
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import require_ident
from kpi_engine.runlog import traced


def default_config_dir() -> Path:
    """Prefer repo-root /config (authoring). Fall back to a packaged copy."""
    here = Path(__file__).resolve()
    repo_config = here.parents[2] / "config"
    if (repo_config / "kpis").is_dir():
        return repo_config
    return here.parents[1] / "config"


@traced
def load_kpi(kpi_id: int | str, config_dir: Path | None = None) -> KpiSpec:
    """Load and parse config/kpis/<kpi_id>.yaml."""
    root = config_dir or default_config_dir()
    path = root / "kpis" / f"{kpi_id}.yaml"
    if not path.exists():
        raise BindError(f"No KPI YAML for kpi_id={kpi_id} at {path}.")
    raw = _read_yaml(path)
    return _parse_kpi(raw, expected_id=kpi_id)


@traced
def load_model(model_id: str, config_dir: Path | None = None) -> ModelSpec:
    """Load and parse config/models/<model_id>.yaml."""
    root = config_dir or default_config_dir()
    path = root / "models" / f"{model_id}.yaml"
    if not path.exists():
        raise BindError(f"No model YAML for model_id={model_id!r} at {path}.")
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


@traced
def assert_measure_keys(kpi: KpiSpec, requested: tuple[str, ...]) -> None:
    """Fail if the context asked for a measure_key not declared in KPI YAML."""
    known = {m.key for m in kpi.measures}
    unknown = [k for k in requested if k not in known]
    if unknown:
        raise BindError(
            f"Unknown measure_key(s) {unknown}. Valid keys: {sorted(known)}."
        )


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
    time_raw = raw.get("time") or {}
    if not isinstance(time_raw, dict):
        raise BindError("time must be an object.")
    grain = time_raw.get("grain", "month")
    if grain not in {"day", "month", "quarter", "year"}:
        raise BindError(f"Unknown time.grain {grain!r}. Use day, month, quarter, or year.")
    calendar = str(time_raw.get("calendar") or "gregorian")
    if calendar not in {"gregorian", "fiscal"}:
        raise BindError(f"Unknown time.calendar {calendar!r}. Use gregorian or fiscal.")
    raw_fiscal_start = time_raw.get("fiscal_start_month")
    fiscal_start = 4 if raw_fiscal_start is None else int(raw_fiscal_start)
    if fiscal_start < 1 or fiscal_start > 12:
        raise BindError("time.fiscal_start_month must be 1-12.")

    dimensions = tuple(
        require_ident(str(d["name"] if isinstance(d, dict) else d), what="dimension")
        for d in raw.get("dimensions") or []
    )

    bases: list[BaseMeasure] = []
    for name, spec in (raw.get("base_measures") or {}).items():
        if not isinstance(spec, dict):
            raise BindError(f"base_measures.{name} must be an object.")
        agg = spec.get("agg", "sum")
        allowed = {"sum", "avg", "count", "min", "max", "count_distinct", "median", "percentile"}
        if agg not in allowed:
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
        model_id = spec.get("model")
        bases.append(
            BaseMeasure(
                name=require_ident(str(name), what="base measure"),
                sql=require_ident(str(spec.get("sql")), what="measure sql"),
                agg=agg,
                model_id=str(model_id) if model_id else None,
                percentile=pvalue,
            )
        )

    cuts = tuple(_parse_cut(c) for c in raw.get("cuts") or [])
    if not cuts:
        raise BindError("At least one cut is required.")
    default_cut = str(raw.get("default_cut") or cuts[0].name)
    if default_cut not in {c.name for c in cuts}:
        raise BindError(f"default_cut {default_cut!r} is not a declared cut.")

    measures = tuple(_parse_measure(k, v) for k, v in (raw.get("measures") or {}).items())
    if not measures:
        raise BindError("measures cannot be empty.")

    filter_map = {
        str(k): require_ident(str(v), what="filter_map column")
        for k, v in (raw.get("filter_map") or {}).items()
    }
    row_set = raw.get("row_set", "span_union")
    if row_set not in {"span_union", "anchor_only"}:
        raise BindError("row_set must be span_union or anchor_only.")

    relations = tuple(_parse_relation(r) for r in raw.get("model_relations") or [])
    base_names = {b.name for b in bases}
    for rel in relations:
        if rel.left not in base_names or rel.right not in base_names:
            raise BindError(
                f"model_relations left/right must be base_measures names "
                f"(got {rel.left!r}, {rel.right!r}; known {sorted(base_names)})."
            )
    model_ids = {b.model_id or str(raw.get("model")) for b in bases}
    if len(model_ids) > 1 and not relations:
        raise BindError(
            "Base measures span multiple models; declare model_relations to join them."
        )

    return KpiSpec(
        kpi_id=kpi_id,
        version=int(raw.get("version") or 1),
        model_id=str(raw.get("model")),
        time=TimeSpec(
            column=require_ident(str(time_raw.get("column")), what="time.column"),
            grain=grain,  # type: ignore[arg-type]
            filter_code=str(time_raw.get("filter_code") or ""),
            calendar=calendar,
            timezone=str(time_raw.get("timezone") or "UTC"),
            fiscal_start_month=fiscal_start,
        ),
        dimensions=dimensions,
        base_measures=tuple(bases),
        cuts=cuts,
        default_cut=default_cut,
        measures=measures,
        filter_map=filter_map,
        row_set=row_set,  # type: ignore[arg-type]
        model_relations=relations,
    )


def _parse_cut(raw: Any) -> CutSpec:
    """Parse one cut: name, group_by dimensions, ignore_filters, also_emit."""
    if not isinstance(raw, dict):
        raise BindError("Each cut must be an object.")
    name = str(raw.get("name") or "")
    if not name:
        raise BindError("Cut name is required.")
    group_by = tuple(
        require_ident(str(c), what="cut group_by") for c in raw.get("group_by") or []
    )
    ignore = tuple(str(x) for x in raw.get("ignore_filters") or [])
    also = tuple(str(x) for x in raw.get("also_emit") or [])
    return CutSpec(name=name, group_by=group_by, ignore_filters=ignore, also_emit=also)


def _parse_relation(raw: Any) -> ModelRelation:
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
    return ModelRelation(
        left=str(raw.get("left") or ""),
        right=str(raw.get("right") or ""),
        on=on,
        how=how,
    )


def _parse_measure(key: str, raw: Any) -> OutputSpec:
    """Parse one requestable measure (point / window / trend / arithmetic / hook / dimension)."""
    if not isinstance(raw, dict):
        raise BindError(f"measures.{key} must be an object.")
    kind = raw.get("kind") or raw.get("op")
    if kind not in {"point", "window", "arithmetic", "trend", "dimension", "hook"}:
        raise BindError(f"measures.{key} has unknown op/kind {kind!r}.")
    offset = None
    if raw.get("offset"):
        off = raw["offset"]
        offset = Offset(
            months=int(off.get("months") or 0),
            years=int(off.get("years") or 0),
            days=int(off.get("days") or 0),
            quarters=int(off.get("quarters") or 0),
        )
    trailing = None
    if raw.get("trailing"):
        trail = raw["trailing"]
        for unit in ("periods", "months", "days", "quarters", "years"):
            if trail.get(unit) is not None:
                trailing = int(trail[unit])
                break
    cuts = tuple(raw["cuts"]) if raw.get("cuts") is not None else None
    hook = raw.get("hook")
    if kind == "hook":
        hook = hook or raw.get("fn")
        if not hook:
            raise BindError(f"measures.{key} op=hook requires `hook:` (an allowlisted name).")
        from kpi_engine.extensions.hooks import REGISTRY

        if str(hook) not in REGISTRY:
            raise BindError(
                f"measures.{key} names unknown hook {hook!r}. "
                "Register it in kpi_engine.extensions.hooks.REGISTRY."
            )
    return OutputSpec(
        key=str(key),
        kind=kind,
        of=raw.get("of"),
        offset=offset,
        trailing_months=trailing,
        inclusive=bool(raw.get("inclusive", True)),
        fn=raw.get("fn"),
        hook=str(hook) if hook else None,
        left=raw.get("left"),
        right=raw.get("right"),
        cuts=cuts,
    )


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


def _optional_path(value: Any) -> str | None:
    """Treat blank YAML paths as missing so context can still supply them."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
