"""Typed data contracts for one compute request.

What this file provides
    Frozen dataclasses: AdaptedRequest, KpiSpec, ModelSpec, TimePlan, BoundFilter,
    OutputSpec (one YAML measure), CutSpec, ExtractResult, and related types.
    No I/O. No DuckDB. No Pandas logic.

Where it is used
    Every core module imports these types. Binder produces KpiSpec/ModelSpec;
    adapter produces AdaptedRequest; time_planner produces TimePlan.

Capabilities
    A single vocabulary so SQL compilation and Pandas calc share the same names
    (grain, cuts, measures, span).

When to use
    Add a field here when YAML or context gains a new locked concept. Do not put
    parsing or SQL in this file — only shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Mapping


AggName = Literal[
    "sum", "avg", "count", "count_distinct", "min", "max", "median", "percentile", "first", "last"
]
GrainName = Literal["day", "week", "month", "quarter", "year"]
GRAIN_NAMES = frozenset({"day", "week", "month", "quarter", "year"})
GRAIN_RANK = {"day": 0, "week": 1, "month": 2, "quarter": 3, "year": 4}
# Open set: any name enabled in registries/ops.yaml.
OpName = str
# Open set: any name registered in registries/functions/column.yaml.
RowOpName = str
WindowRangeName = Literal[
    "trailing",
    "leading",
    "cumulative",
    "ytd",
    "mtd",
    "qtd",
    "wtd",
    "full_month",
    "full_quarter",
    "full_year",
]
PTD_RANGES = frozenset({"mtd", "qtd", "ytd", "wtd", "cumulative"})
FULL_RANGES = frozenset({"full_month", "full_quarter", "full_year"})
NAMED_WINDOW_RANGES = PTD_RANGES | FULL_RANGES
WINDOW_RANGE_NAMES = frozenset(
    {"trailing", "leading", "cumulative", "ytd", "mtd", "qtd", "wtd", "full_month", "full_quarter", "full_year"}
)
NON_ADDITIVE_AGGS = frozenset({"count_distinct", "median", "percentile", "first", "last"})
OVER_FNS = frozenset(
    {
        "lag",
        "lead",
        "row_number",
        "rank",
        "dense_rank",
        "running_sum",
        "running_avg",
        "last_n",
    }
)
WINDOW_AGGS_NEED_IDENTITY = frozenset({"sum", "avg", "count", "count_distinct"})
HAVING_CMP = frozenset({"gt", "gte", "lt", "lte", "eq", "ne", "between"})


@dataclass(frozen=True)
class DatasetBinding:
    """One dataset from context.datasets, ready for DuckDB to scan."""

    key: str
    alias: str
    path: str
    table_type: str
    columns: tuple[str, ...]
    mappings: tuple["FilterMapping", ...]


@dataclass(frozen=True)
class FilterMapping:
    """Maps a context filter_code to a physical column (operator defaults to IN)."""

    filter_code: str
    column_name: str
    operator: str
    view_id: int | None = None


@dataclass(frozen=True)
class IncomingFilter:
    """A filter as it arrived on the context, before column binding."""

    raw_key: str
    code: str
    values: tuple[Any, ...]
    input_text: str | None


FilterStage = Literal["extract", "calc", "result"]


@dataclass(frozen=True)
class FilterApplySpec:
    """KPI YAML `filters:` entry: how a context code is applied, and where."""

    code: str
    column: str
    op: str = "in"
    optional: bool = False
    apply: FilterStage = "extract"
    compose_template: str | None = None


@dataclass(frozen=True)
class BoundFilter:
    """Filter bound to a column, operator, and apply stage (extract / calc / result)."""

    code: str
    column: str
    values: tuple[Any, ...]
    stage: FilterStage
    op: str = "in"
    optional: bool = False
    input_text: str | None = None


@dataclass(frozen=True)
class Pagination:
    """Caller paging settings. Null page_size means return every row."""

    page: int | None
    page_size: int | None
    limit: int | None


@dataclass(frozen=True)
class ParameterSpec:
    """One KPI YAML `parameters:` entry: type, default, aliases, allowlist, list item type."""

    name: str
    type_name: str
    default: Any = None
    has_default: bool = False
    allowed: tuple[Any, ...] | None = None
    value_map: Mapping[Any, Any] = field(default_factory=dict)
    item_type: str | None = None


@dataclass(frozen=True)
class BoundParameters:
    """Request parameters after schema bind, before YAML resolve."""

    values: Mapping[str, Any] = field(default_factory=dict)
    schema: tuple[ParameterSpec, ...] = ()
    locked_cut: str | None = None
    model_templated: bool = False


@dataclass(frozen=True)
class AdaptedRequest:
    """Normalized request after parsing context; still independent of KPI YAML."""

    kpi_id: int | str
    request_id: str | None
    measure_keys: tuple[str, ...]
    filters: tuple[IncomingFilter, ...]
    datasets: tuple[DatasetBinding, ...]
    pagination: Pagination
    raw: dict[str, Any]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    measures_omitted: bool = False
    selected_dimensions: tuple[str, ...] | Mapping[str, bool] | None = None


@dataclass(frozen=True)
class TimeSpec:
    """KPI time column, grain, and which context filter is the selected period.

    Absent on snapshot KPIs that omit the YAML `time:` block.
    """

    column: str
    grain: GrainName
    filter_code: str
    calendar: str = "gregorian"
    fiscal_start_month: int = 4
    format: str | None = None
    compose_template: str | None = None
    source_grain: GrainName | None = None
    grains: tuple[GrainName, ...] = ()


@dataclass(frozen=True)
class MeasureWhere:
    """Structured row mask on a retrieved column (Pandas, not SQL)."""

    column: str
    op: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class DimensionSpec:
    """A grouping field, optionally mapped or truncated after the extract."""

    name: str
    source: str
    mapping: dict[str, str] = field(default_factory=dict)
    default: str | None = None
    grain: GrainName | None = None
    cardinality: str | None = None


@dataclass(frozen=True)
class LookupSpec:
    """Static map from one retrieved column onto a numeric/string value."""

    column: str
    mapping: Mapping[str, Any]
    default: Any = None
    strict: bool = False


@dataclass(frozen=True)
class OverSpec:
    """Entity window on the pre-fold detail frame (not calendar op: lag)."""

    fn: str
    partition_by: tuple[str, ...]
    order_by: tuple[str, ...]
    of: str | None = None
    n: int | None = None


@dataclass(frozen=True)
class HavingPredicate:
    """One measure comparison used by having: or op: predicate."""

    of: str
    cmp: str
    value: float | None = None
    vs: str | None = None
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class HavingSpec:
    """Drop (or flag) cut groups by measure predicates; optional coarser re-fold."""

    predicates: tuple[HavingPredicate, ...]
    match: Literal["all", "any"] = "all"
    then_group_by: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BaseMeasure:
    """Internal fact from the extract, optionally combined in Pandas."""

    name: str
    sql: str
    agg: AggName | None
    model_id: str | None = None
    percentile: float | None = None
    columns: tuple[str, ...] = ()
    column_params: tuple[str, ...] = ()
    row_op: RowOpName | None = None
    where: MeasureWhere | None = None
    expr: str | None = None
    lookup: LookupSpec | None = None
    over: OverSpec | None = None
    replace: bool = False
    agg_ok: bool = False


@dataclass(frozen=True)
class CutSpec:
    """One grouping grain (e.g. global vs region) plus filters to ignore."""

    name: str
    group_by: tuple[str, ...]
    ignore_filters: tuple[str, ...]
    also_emit: tuple[str, ...]
    exclude_from_grain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Offset:
    """Calendar offset for a point measure (months and years added together)."""

    months: int = 0
    years: int = 0
    days: int = 0
    quarters: int = 0
    weeks: int = 0

    @property
    def total_months(self) -> int:
        """Return the offset as a single month count (years * 12 + months)."""
        return self.years * 12 + self.months


@dataclass(frozen=True)
class OutputSpec:
    """One requestable measure (point, window, trend, arithmetic, fn, expr, hook, constant, rank, percent_of_total, or dimension)."""

    key: str
    kind: OpName
    of: str | None = None
    offset: Offset | None = None
    trailing_months: int | None = None
    trailing_from: str | None = None
    inclusive: bool = True
    fn: str | None = None
    hook: str | None = None
    left: str | None = None
    right: str | None = None
    cuts: tuple[str, ...] | None = None
    window_range: WindowRangeName | None = None
    trailing_unit: GrainName | None = None
    operands: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    input_params: tuple[str, ...] = ()
    expr: str | None = None
    constant: float | None = None
    rank_order: str | None = None
    rank_group_by: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KpiSpec:
    """Parsed KPI YAML: model, time, dimensions, base facts, cuts, and measures."""

    kpi_id: int | str
    version: int
    model_id: str
    time: TimeSpec | None
    dimensions: tuple[str, ...]
    base_measures: tuple[BaseMeasure, ...]
    cuts: tuple[CutSpec, ...]
    default_cut: str
    measures: tuple[OutputSpec, ...]
    filter_map: dict[str, str] = field(default_factory=dict)
    filter_specs: tuple[FilterApplySpec, ...] = ()
    row_set: Literal["span_union", "anchor_only"] = "span_union"
    model_relations: tuple["ModelRelation", ...] = ()
    dimension_specs: tuple[DimensionSpec, ...] = ()
    data_points: int | Mapping[str, int] | None = None
    meta: "KpiMeta | None" = None
    green_when: "GreenWhen | None" = None
    parameter_schema: tuple[ParameterSpec, ...] = ()
    bound_parameters: Mapping[str, Any] = field(default_factory=dict)
    locked_cut: str | None = None
    model_templated: bool = False
    default_dimensions: tuple[str, ...] = ()
    request_grain: tuple[str, ...] = ()
    having: HavingSpec | None = None


@dataclass(frozen=True)
class KpiMeta:
    """Literal KPI master fields copied onto the response."""

    kpi: str | None = None
    parent_kpi: str | None = None
    is_child: bool | None = None
    selected_metrics: tuple[str, ...] = ()


@dataclass(frozen=True)
class GreenWhen:
    """Row is green when the named measure is at or beyond this threshold."""

    of: str
    above: float | None = None
    below: float | None = None


@dataclass(frozen=True)
class PhysicalSource:
    """Named source in a physical or SQL model, bound to a context dataset alias."""

    name: str
    alias: str
    default_path: str | None = None
    table_type: str = "PARQUET"


@dataclass(frozen=True)
class JoinSpec:
    """Join between two physical sources on listed key columns."""

    left: str
    right: str
    on: tuple[str, ...]
    join_type: str = "left"


@dataclass(frozen=True)
class ModelRelation:
    """Join two base measures after each model's extract (not a SQL join of raw rows)."""

    left: str
    right: str
    on: tuple[str, ...]
    how: str = "outer"


@dataclass(frozen=True)
class ModelSpec:
    """Parsed model YAML: physical tables/joins or a SQL/CTE query."""

    model_id: str
    kind: Literal["physical", "sql"]
    required_aliases: tuple[str, ...]
    sources: tuple[PhysicalSource, ...] = ()
    joins: tuple[JoinSpec, ...] = ()
    sql: str | None = None
    output_schema: tuple[str, ...] = ()
    default_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TimePlan:
    """Anchor month and the widened scan range needed for requested lookbacks."""

    anchor: date
    span_start: date
    span_end_exclusive: date
    lookback_months: int
    claimed_filter_code: str
    lookback_forward: int = 0


@dataclass(frozen=True)
class ExtractResult:
    """DuckDB result: aggregated monthly frame plus the SQL that produced it."""

    frame: Any
    sql: str
    params: tuple[Any, ...]
