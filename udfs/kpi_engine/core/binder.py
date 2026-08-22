"""Load KPI/model YAML and bind context datasets by alias.

What this file provides
    default_config_dir, load_kpi, load_model, bind_datasets, assert_measure_keys,
    resolve_requested_graph, same_model_id. Parsers for cuts, measures, models.

Where it is used
    orchestrator after adapt(). Tests load KPI 3004 via load_kpi.

Capabilities
    - Reads config/kpis/<kpi_id>.yaml and config/models/<model_id>.yaml.
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

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from kpi_engine.contracts import (
    AdaptedRequest,
    BaseMeasure,
    CutSpec,
    DatasetBinding,
    DimensionSpec,
    JoinSpec,
    KpiSpec,
    MeasureWhere,
    ModelRelation,
    ModelSpec,
    Offset,
    OutputSpec,
    PhysicalSource,
    TimeSpec,
)
from kpi_engine.exceptions import BindError
from kpi_engine.identifiers import (
    compile_sql_expr,
    expression_columns,
    is_simple_ident,
    norm_name,
    parse_expression,
    require_ident,
)
from kpi_engine.catalog.ops_impl import (
    COLUMN_FNS,
    MEASURE_FNS,
    WHERE_OPS,
    column_op_error,
    measure_fn_error,
)
from kpi_engine.runlog import traced


def default_config_dir() -> Path:
    """Prefer udfs/config (next to kpi_engine). Fall back to a packaged copy."""
    here = Path(__file__).resolve()
    udfs_config = here.parents[2] / "config"
    if (udfs_config / "kpis").is_dir():
        return udfs_config
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
    """Load and parse config/models/<model_id>.yaml (name fold: Sotif → sotif.yaml)."""
    root = config_dir or default_config_dir()
    path = root / "models" / f"{model_id}.yaml"
    if not path.exists():
        wanted = norm_name(model_id)
        matches = [
            candidate
            for candidate in (root / "models").glob("*.yaml")
            if norm_name(candidate.stem) == wanted
        ]
        if len(matches) == 1:
            path = matches[0]
        else:
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


@traced
def resolve_requested_graph(
    kpi: KpiSpec, requested: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[BaseMeasure, ...]]:
    """Requested keys (no catalog fallback) and the base_measures that graph needs.

    Walks arithmetic / fn / expr / rank `of` and each measure's `of:` base.
    An empty measures_required list computes nothing — it does not expand to
    every key in the KPI YAML.
    """
    emit = tuple(dict.fromkeys(requested))
    by_key = {m.key: m for m in kpi.measures}
    by_base = {b.name: b for b in kpi.base_measures}
    needed: set[str] = set()
    seen: set[str] = set()

    def walk(name: str) -> None:
        """Collect this key's bases and the measures it depends on."""
        if name in seen:
            return
        seen.add(name)
        if name in by_base:
            needed.add(name)
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

    dim_specs = tuple(_parse_dimension(d) for d in raw.get("dimensions") or [])
    dimensions = tuple(d.name for d in dim_specs)

    bases: list[BaseMeasure] = []
    for name, spec in (raw.get("base_measures") or {}).items():
        bases.append(_parse_base_measure(name, spec))

    cuts = tuple(_parse_cut(c) for c in raw.get("cuts") or [])
    if not cuts:
        raise BindError("At least one cut is required.")
    default_cut = str(raw.get("default_cut") or cuts[0].name)
    if default_cut not in {c.name for c in cuts}:
        raise BindError(f"default_cut {default_cut!r} is not a declared cut.")

    measures = tuple(_parse_measure(k, v) for k, v in (raw.get("measures") or {}).items())
    if not measures:
        raise BindError("measures cannot be empty.")
    _assert_measure_graph(measures, tuple(b.name for b in bases), dimensions)

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

    if time is None:
        _assert_snapshot_measures(measures)

    return KpiSpec(
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
        row_set=row_set,  # type: ignore[arg-type]
        model_relations=relations,
        dimension_specs=dim_specs,
    )


def _parse_dimension(raw: Any) -> DimensionSpec:
    """Parse a dimension name or {name, from, map, default, grain}."""
    if isinstance(raw, str):
        name = require_ident(raw, what="dimension")
        return DimensionSpec(name=name, source=name)
    if not isinstance(raw, dict) or not raw.get("name"):
        raise BindError("Each dimension needs a name.")
    name = require_ident(str(raw["name"]), what="dimension")
    source = require_ident(str(raw.get("from") or name), what="dimension.from")
    mapping_raw = raw.get("map") or {}
    if mapping_raw and not isinstance(mapping_raw, dict):
        raise BindError(f"dimensions.{name}.map must be an object.")
    mapping = {str(k): str(v) for k, v in mapping_raw.items()}
    grain = raw.get("grain")
    if grain is not None and grain not in {"day", "month", "quarter", "year"}:
        raise BindError(f"dimensions.{name}.grain must be day, month, quarter, or year.")
    default = raw.get("default")
    return DimensionSpec(
        name=name,
        source=source,
        mapping=mapping,
        default=None if default is None else str(default),
        grain=grain,
    )


def _parse_base_measure(name: str, spec: Any) -> BaseMeasure:
    """Parse sql:/columns:/op:/expr:/agg:/where for one base fact."""
    if not isinstance(spec, dict):
        raise BindError(f"base_measures.{name} must be an object.")
    columns, column_params = _parse_column_args(name, spec.get("columns"))
    row_op = spec.get("op") or spec.get("row_op")
    expr_raw = spec.get("expr")
    expr = str(expr_raw).strip() if expr_raw else None
    if expr:
        parse_expression(expr, what=f"base_measures.{name}.expr")
        if row_op is not None or columns:
            raise BindError(
                f"base_measures.{name} uses `expr:` and cannot also set `columns:` / `op:`."
            )
        if spec.get("sql"):
            raise BindError(
                f"base_measures.{name} uses `expr:` and cannot also set `sql:`."
            )
        columns = expression_columns(parse_expression(expr, what=f"base_measures.{name}.expr"))
    if row_op is not None:
        row_op = str(row_op)
        if row_op not in COLUMN_FNS:
            raise BindError(
                f"base_measures.{name} unknown op {row_op!r}. Registered: "
                f"{sorted(COLUMN_FNS)}. Register it with "
                "kpi_engine.extensions.functions.register_column_fn."
            )
    elif column_params:
        raise BindError(
            f"base_measures.{name} names its columns but has no `op:` to bind them to."
        )
    sql_raw = str(spec.get("sql") or "").strip()
    if not sql_raw and columns:
        sql_raw = columns[0]
    if not sql_raw and expr:
        sql_raw = columns[0] if columns else ""
    if not sql_raw:
        raise BindError(f"base_measures.{name} needs sql:, columns:, or expr:.")
    if sql_raw:
        compile_sql_expr(sql_raw, what="measure sql")
    if row_op is not None:
        problem = column_op_error(row_op, len(columns) or 1, column_params)
        if problem:
            raise BindError(f"base_measures.{name} op {problem}")
    if not expr and sql_raw and not is_simple_ident(sql_raw) and row_op is None:
        expr = sql_raw
        columns = expression_columns(parse_expression(expr, what="measure sql"))
    default_agg = "sum" if row_op is None else spec.get("agg")
    agg = spec.get("agg", default_agg)
    if agg is None:
        agg = "sum"
    allowed = {
        "sum", "avg", "count", "min", "max", "count_distinct", "median", "percentile", "first", "last"
    }
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
    where = _parse_where(name, spec.get("where"))
    model_id = spec.get("model")
    return BaseMeasure(
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
    )


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
    op = str(raw.get("op") or "in").lower()
    if op not in WHERE_OPS:
        raise BindError(f"base_measures.{name}.where.op must be in, eq, or ne.")
    values_raw = raw.get("values")
    if values_raw is None and "value" in raw:
        values_raw = [raw["value"]]
    if values_raw is None:
        raise BindError(f"base_measures.{name}.where needs values:.")
    if not isinstance(values_raw, (list, tuple)):
        values_raw = [values_raw]
    return MeasureWhere(column=column, op=op, values=tuple(values_raw))


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
    if grain not in {"day", "month", "quarter", "year"}:
        raise BindError(f"Unknown time.grain {grain!r}. Use day, month, quarter, or year.")
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
    return TimeSpec(
        column=require_ident(str(column), what="time.column"),
        grain=grain,  # type: ignore[arg-type]
        filter_code=filter_code,
        calendar=calendar,
        fiscal_start_month=fiscal_start,
        format=time_format,
    )


def measure_dependencies(spec: OutputSpec) -> tuple[str, ...]:
    """Measure keys this measure consumes (fn/expr inputs, arithmetic operands)."""
    if spec.kind in {"fn", "expr"}:
        return spec.inputs
    if spec.kind == "arithmetic":
        if spec.operands:
            return spec.operands
        return tuple(n for n in (spec.left, spec.right) if n)
    if spec.kind == "rank" and spec.of:
        return (spec.of,)
    return ()


def _assert_measure_graph(
    measures: tuple[OutputSpec, ...],
    base_names: tuple[str, ...],
    dimensions: tuple[str, ...],
) -> None:
    """Fail at bind time on unknown references and dependency cycles."""
    by_key = {m.key: m for m in measures}
    known_bases = set(base_names)
    for spec in measures:
        if spec.kind in {"point", "window", "trend"} and not spec.of:
            raise BindError(
                f"measures.{spec.key} op={spec.kind} requires `of:` naming the base "
                f"measure it aggregates. Declared base_measures: {sorted(known_bases)}."
            )
        if spec.kind == "rank" and not spec.of:
            raise BindError(f"measures.{spec.key} op=rank requires `of:`.")
        if spec.kind in {"point", "window", "trend", "hook"} and spec.of:
            if spec.of not in known_bases:
                raise BindError(
                    f"measures.{spec.key} of={spec.of!r} is not a base measure. "
                    f"Declared base_measures: {sorted(known_bases)}."
                )
        if spec.kind == "rank" and spec.of:
            if spec.of not in by_key and spec.of not in known_bases:
                raise BindError(
                    f"measures.{spec.key} of={spec.of!r} is not a measure or base "
                    f"measure. Declared measures: {sorted(by_key)}; "
                    f"base_measures: {sorted(known_bases)}."
                )
        if spec.kind == "dimension" and spec.key not in dimensions:
            raise BindError(
                f"measures.{spec.key} is a dimension measure but {spec.key!r} is not in "
                f"dimensions: {sorted(dimensions)}."
            )
        for name in measure_dependencies(spec):
            if name in by_key:
                continue
            if spec.kind == "rank" and name in known_bases:
                continue
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
        if spec.kind in {"window", "trend"}:
            bad.append(f"{spec.key} ({spec.kind})")
        elif spec.kind == "point" and _offset_is_nonzero(spec.offset):
            bad.append(f"{spec.key} (point offset)")
        elif spec.kind == "hook" and (
            _offset_is_nonzero(spec.offset) or spec.trailing_months
        ):
            bad.append(f"{spec.key} (hook lookback)")
    if bad:
        raise BindError(
            "This KPI has no time: block, so measures cannot use windows, trends, "
            f"or period offsets ({', '.join(bad)}). Add time.column / time.filter_code, "
            "or keep only current-period point / dimension / arithmetic measures."
        )


def _offset_is_nonzero(offset: Offset | None) -> bool:
    """True when a point/hook offset actually shifts the anchor."""
    if offset is None:
        return False
    return bool(offset.months or offset.years or offset.days or offset.quarters)


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
    """Parse one requestable measure (point / window / trend / arithmetic / fn / hook / dimension)."""
    if not isinstance(raw, dict):
        raise BindError(f"measures.{key} must be an object.")
    kind = raw.get("kind") or raw.get("op")
    if kind not in {
        "point",
        "window",
        "arithmetic",
        "trend",
        "dimension",
        "hook",
        "fn",
        "expr",
        "constant",
        "rank",
    }:
        raise BindError(f"measures.{key} has unknown op/kind {kind!r}.")
    if raw.get("fn") is not None and kind in {
        "point",
        "window",
        "trend",
        "dimension",
        "expr",
        "constant",
        "rank",
    }:
        raise BindError(
            f"measures.{key} op={kind} ignores `fn:`. "
            "Use op: fn with inputs:, or op: arithmetic, or op: hook."
        )
    if raw.get("inputs") is not None and kind not in {"fn", "expr"}:
        raise BindError(f"measures.{key} op={kind} ignores `inputs:`. Use op: fn or op: expr.")
    if raw.get("expr") is not None and kind not in {"expr"}:
        raise BindError(f"measures.{key} op={kind} ignores `expr:`. Use op: expr.")
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
    trailing_unit = None
    if raw.get("trailing"):
        trail = raw["trailing"]
        for unit in ("periods", "months", "days", "quarters", "years"):
            if trail.get(unit) is not None:
                trailing = int(trail[unit])
                trailing_unit = {
                    "periods": None,
                    "months": "month",
                    "days": "day",
                    "quarters": "quarter",
                    "years": "year",
                }[unit]
                break
    window_range = raw.get("range")
    if window_range is not None:
        window_range = str(window_range)
        if window_range not in {"trailing", "leading", "cumulative"}:
            raise BindError(
                f"measures.{key}.range must be trailing, leading, or cumulative."
            )
    of_raw = raw.get("of")
    of = None
    operands: tuple[str, ...] = ()
    if isinstance(of_raw, (list, tuple)):
        operands = tuple(str(x) for x in of_raw)
    elif of_raw is not None:
        of = str(of_raw)
    inputs: tuple[str, ...] = ()
    input_params: tuple[str, ...] = ()
    if kind == "fn":
        raw_inputs = raw.get("inputs")
        if not raw_inputs:
            raise BindError(
                f"measures.{key} op=fn requires `inputs:` listing the measures to feed it."
            )
        inputs, input_params = _parse_fn_inputs(key, raw_inputs)
        if not raw.get("fn"):
            raise BindError(
                f"measures.{key} op=fn requires `fn:` (a registered measure function)."
            )
        fn_name = str(raw["fn"])
        if fn_name not in MEASURE_FNS:
            raise BindError(
                f"measures.{key} names unknown fn {fn_name!r}. Registered: "
                f"{sorted(MEASURE_FNS)}. Register it with "
                "kpi_engine.extensions.functions.register_measure_fn."
            )
        problem = measure_fn_error(fn_name, len(inputs), input_params)
        if problem:
            raise BindError(f"measures.{key} fn {problem}")
    if kind == "arithmetic":
        arithmetic_fn = str(raw.get("fn") or "divide")
        if arithmetic_fn not in MEASURE_FNS:
            raise BindError(
                f"measures.{key} names unknown fn {arithmetic_fn!r}. Registered: "
                f"{sorted(MEASURE_FNS)}. Register it with "
                "kpi_engine.extensions.functions.register_measure_fn."
            )
        operand_count = len(operands) or len([n for n in (raw.get("left"), raw.get("right")) if n])
        problem = measure_fn_error(arithmetic_fn, operand_count or 2)
        if problem:
            raise BindError(f"measures.{key} fn {problem}")
    expr = None
    if kind == "expr":
        expr_raw = raw.get("expr")
        if not expr_raw or not str(expr_raw).strip():
            raise BindError(f"measures.{key} op=expr requires `expr:` with a formula.")
        expr = str(expr_raw).strip()
        node = parse_expression(expr, what=f"measures.{key}.expr")
        names = expression_columns(node)
        if not names:
            raise BindError(f"measures.{key} expr must name at least one other measure.")
        if raw.get("inputs"):
            inputs, input_params = _parse_fn_inputs(key, raw.get("inputs"))
            bindable = input_params or inputs
            unknown = [n for n in names if n not in bindable]
            if unknown:
                raise BindError(
                    f"measures.{key} expr names {unknown[0]!r}, which is not in inputs: {list(bindable)}."
                )
        else:
            inputs, input_params = names, ()
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
    constant = None
    if kind == "constant":
        if raw.get("value") is None:
            raise BindError(f"measures.{key} op=constant requires `value:`.")
        try:
            constant = float(raw["value"])
        except (TypeError, ValueError) as exc:
            raise BindError(
                f"measures.{key} op=constant value must be a number (got {raw.get('value')!r})."
            ) from exc
    rank_order = None
    rank_group_by: tuple[str, ...] = ()
    if kind == "rank":
        order = str(raw.get("order") or "desc").strip().lower()
        if order not in {"asc", "desc"}:
            raise BindError(f"measures.{key} op=rank order must be asc or desc.")
        rank_order = order
        raw_group = raw.get("group_by") or []
        if not isinstance(raw_group, (list, tuple)):
            raise BindError(f"measures.{key} op=rank group_by must be a list.")
        rank_group_by = tuple(require_ident(str(c), what="rank group_by") for c in raw_group)
    return OutputSpec(
        key=str(key),
        kind=kind,
        of=of,
        offset=offset,
        trailing_months=trailing,
        inclusive=bool(raw.get("inclusive", True)),
        fn=raw.get("fn"),
        hook=str(hook) if hook else None,
        left=raw.get("left"),
        right=raw.get("right"),
        cuts=cuts,
        window_range=window_range,
        trailing_unit=trailing_unit,
        operands=operands,
        inputs=inputs,
        input_params=input_params,
        expr=expr,
        constant=constant,
        rank_order=rank_order,
        rank_group_by=rank_group_by,
    )


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


def _optional_path(value: Any) -> str | None:
    """Treat blank YAML paths as missing so context can still supply them."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
