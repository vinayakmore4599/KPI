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
from typing import Any, Literal


AggName = Literal[
    "sum", "avg", "count", "count_distinct", "min", "max", "median", "percentile", "first", "last"
]
OpName = Literal["point", "window", "arithmetic", "trend", "dimension", "hook", "fn", "expr"]
GrainName = Literal["day", "month", "quarter", "year"]
# Open set: any name registered in catalog.ops_impl.COLUMN_FNS.
RowOpName = str
WindowRangeName = Literal["trailing", "leading", "cumulative"]


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


@dataclass(frozen=True)
class BoundFilter:
    """Filter bound to a column and a stage (source DuckDB, or per-cut Pandas)."""

    code: str
    column: str
    values: tuple[Any, ...]
    stage: Literal["source", "target", "cut"]
    input_text: str | None = None


@dataclass(frozen=True)
class Pagination:
    """Caller paging settings. Null page_size means return every row."""

    page: int | None
    page_size: int | None
    limit: int | None


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


@dataclass(frozen=True)
class BaseMeasure:
    """Internal fact from the extract, optionally combined in Pandas."""

    name: str
    sql: str
    agg: AggName
    model_id: str | None = None
    percentile: float | None = None
    columns: tuple[str, ...] = ()
    column_params: tuple[str, ...] = ()
    row_op: RowOpName | None = None
    where: MeasureWhere | None = None
    expr: str | None = None


@dataclass(frozen=True)
class CutSpec:
    """One grouping grain (e.g. global vs region) plus filters to ignore."""

    name: str
    group_by: tuple[str, ...]
    ignore_filters: tuple[str, ...]
    also_emit: tuple[str, ...]


@dataclass(frozen=True)
class Offset:
    """Calendar offset for a point measure (months and years added together)."""

    months: int = 0
    years: int = 0
    days: int = 0
    quarters: int = 0

    @property
    def total_months(self) -> int:
        """Return the offset as a single month count (years * 12 + months)."""
        return self.years * 12 + self.months


@dataclass(frozen=True)
class OutputSpec:
    """One requestable measure (point, window, trend, arithmetic, fn, expr, hook, or dimension)."""

    key: str
    kind: OpName
    of: str | None = None
    offset: Offset | None = None
    trailing_months: int | None = None
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
    row_set: Literal["span_union", "anchor_only"] = "span_union"
    model_relations: tuple["ModelRelation", ...] = ()
    dimension_specs: tuple[DimensionSpec, ...] = ()


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
