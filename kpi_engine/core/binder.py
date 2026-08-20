"""Load KPI and model YAML and bind context datasets by alias."""

from __future__ import annotations

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
    ModelSpec,
    Offset,
    OutputSpec,
    PhysicalSource,
    TimeSpec,
)
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import require_ident


def default_config_dir() -> Path:
    """Prefer repo-root /config (authoring). Fall back to a packaged copy."""
    here = Path(__file__).resolve()
    repo_config = here.parents[2] / "config"
    if (repo_config / "kpis").is_dir():
        return repo_config
    return here.parents[1] / "config"


def load_kpi(kpi_id: int | str, config_dir: Path | None = None) -> KpiSpec:
    root = config_dir or default_config_dir()
    path = root / "kpis" / f"{kpi_id}.yaml"
    if not path.exists():
        raise BindError(f"No KPI YAML for kpi_id={kpi_id} at {path}.")
    raw = _read_yaml(path)
    return _parse_kpi(raw, expected_id=kpi_id)


def load_model(model_id: str, config_dir: Path | None = None) -> ModelSpec:
    root = config_dir or default_config_dir()
    path = root / "models" / f"{model_id}.yaml"
    if not path.exists():
        raise BindError(f"No model YAML for model_id={model_id!r} at {path}.")
    return _parse_model(_read_yaml(path), expected_id=model_id)


def bind_datasets(
    model: ModelSpec, request: AdaptedRequest
) -> dict[str, DatasetBinding]:
    by_alias = {d.alias.lower(): d for d in request.datasets}
    by_key = {d.key.lower(): d for d in request.datasets}
    bound: dict[str, DatasetBinding] = {}
    for alias in model.required_aliases:
        hit = by_alias.get(alias.lower()) or by_key.get(alias.lower())
        if hit is None:
            available = sorted({*(d.alias for d in request.datasets), *(d.key for d in request.datasets)})
            raise BindError(
                f"Model {model.model_id!r} requires alias {alias!r}; "
                f"context datasets have {available}."
            )
        bound[alias] = hit
    return bound


def assert_measure_keys(kpi: KpiSpec, requested: tuple[str, ...]) -> None:
    known = {o.key for o in kpi.outputs}
    unknown = [k for k in requested if k not in known]
    if unknown:
        raise BindError(
            f"Unknown measure_key(s) {unknown}. Valid keys: {sorted(known)}."
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise BindError(f"{path} must contain a YAML object.")
    return raw


def _parse_kpi(raw: dict[str, Any], expected_id: int | str) -> KpiSpec:
    kpi_id = raw.get("kpi_id", expected_id)
    time_raw = raw.get("time") or {}
    if not isinstance(time_raw, dict):
        raise BindError("time must be an object.")
    grain = time_raw.get("grain", "month")
    if grain != "month":
        raise BindError("First slice supports time.grain: month only.")

    dimensions = tuple(
        require_ident(str(d["name"] if isinstance(d, dict) else d), what="dimension")
        for d in raw.get("dimensions") or []
    )

    bases: list[BaseMeasure] = []
    for name, spec in (raw.get("base_measures") or {}).items():
        if not isinstance(spec, dict):
            raise BindError(f"base_measures.{name} must be an object.")
        agg = spec.get("agg", "sum")
        if agg not in {"sum", "avg", "count", "min", "max"}:
            if agg in {"count_distinct", "median", "percentile"}:
                raise BindError(
                    f"agg {agg!r} is not supported in this slice "
                    "(non-additive measures need per-cut DuckDB scans)."
                )
            raise BindError(f"Unknown agg {agg!r} on {name}.")
        bases.append(
            BaseMeasure(
                name=require_ident(str(name), what="base measure"),
                sql=require_ident(str(spec.get("sql")), what="measure sql"),
                agg=agg,
            )
        )

    cuts = tuple(_parse_cut(c) for c in raw.get("cuts") or [])
    if not cuts:
        raise BindError("At least one cut is required.")
    default_cut = str(raw.get("default_cut") or cuts[0].name)
    if default_cut not in {c.name for c in cuts}:
        raise BindError(f"default_cut {default_cut!r} is not a declared cut.")

    outputs = tuple(_parse_output(k, v) for k, v in (raw.get("outputs") or {}).items())
    if not outputs:
        raise BindError("outputs cannot be empty.")

    filter_map = {
        str(k): require_ident(str(v), what="filter_map column")
        for k, v in (raw.get("filter_map") or {}).items()
    }
    row_set = raw.get("row_set", "span_union")
    if row_set not in {"span_union", "anchor_only"}:
        raise BindError("row_set must be span_union or anchor_only.")

    return KpiSpec(
        kpi_id=kpi_id,
        version=int(raw.get("version") or 1),
        model_id=str(raw.get("model")),
        time=TimeSpec(
            column=require_ident(str(time_raw.get("column")), what="time.column"),
            grain="month",
            filter_code=str(time_raw.get("filter_code") or ""),
            calendar=str(time_raw.get("calendar") or "gregorian"),
            timezone=str(time_raw.get("timezone") or "UTC"),
        ),
        dimensions=dimensions,
        base_measures=tuple(bases),
        cuts=cuts,
        default_cut=default_cut,
        outputs=outputs,
        filter_map=filter_map,
        row_set=row_set,  # type: ignore[arg-type]
    )


def _parse_cut(raw: Any) -> CutSpec:
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


def _parse_output(key: str, raw: Any) -> OutputSpec:
    if not isinstance(raw, dict):
        raise BindError(f"outputs.{key} must be an object.")
    kind = raw.get("kind") or raw.get("op")
    if kind not in {"point", "window", "arithmetic", "trend", "dimension"}:
        raise BindError(f"outputs.{key} has unknown op/kind {kind!r}.")
    offset = None
    if raw.get("offset"):
        off = raw["offset"]
        offset = Offset(months=int(off.get("months") or 0), years=int(off.get("years") or 0))
    trailing = None
    if raw.get("trailing"):
        trailing = int(raw["trailing"].get("months"))
    cuts = tuple(raw["cuts"]) if raw.get("cuts") is not None else None
    return OutputSpec(
        key=str(key),
        kind=kind,
        of=raw.get("of"),
        offset=offset,
        trailing_months=trailing,
        inclusive=bool(raw.get("inclusive", True)),
        fn=raw.get("fn"),
        left=raw.get("left"),
        right=raw.get("right"),
        cuts=cuts,
    )


def _parse_model(raw: dict[str, Any], expected_id: str) -> ModelSpec:
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
    return ModelSpec(
        model_id=model_id,
        kind=kind,  # type: ignore[arg-type]
        required_aliases=aliases,
        sources=tuple(sources),
        joins=tuple(joins),
        sql=sql,
        output_schema=schema,
    )
