"""Typed contracts for a single compute request. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal


AggName = Literal["sum", "avg", "count", "count_distinct", "min", "max"]
OpName = Literal["point", "window", "arithmetic", "trend", "dimension"]
GrainName = Literal["day", "month", "quarter", "year"]


@dataclass(frozen=True)
class DatasetBinding:
    key: str
    alias: str
    path: str
    table_type: str
    columns: tuple[str, ...]
    mappings: tuple["FilterMapping", ...]


@dataclass(frozen=True)
class FilterMapping:
    filter_code: str
    column_name: str
    operator: str
    view_id: int | None = None


@dataclass(frozen=True)
class IncomingFilter:
    raw_key: str
    code: str
    values: tuple[Any, ...]
    input_text: str | None


@dataclass(frozen=True)
class BoundFilter:
    code: str
    column: str
    values: tuple[Any, ...]
    stage: Literal["source", "target", "cut"]
    input_text: str | None = None


@dataclass(frozen=True)
class Pagination:
    page: int | None
    page_size: int | None
    limit: int | None


@dataclass(frozen=True)
class AdaptedRequest:
    kpi_id: int | str
    request_id: str | None
    measure_keys: tuple[str, ...]
    filters: tuple[IncomingFilter, ...]
    datasets: tuple[DatasetBinding, ...]
    pagination: Pagination
    raw: dict[str, Any]


@dataclass(frozen=True)
class TimeSpec:
    column: str
    grain: GrainName
    filter_code: str
    calendar: str = "gregorian"
    timezone: str = "UTC"


@dataclass(frozen=True)
class BaseMeasure:
    name: str
    sql: str
    agg: AggName


@dataclass(frozen=True)
class CutSpec:
    name: str
    group_by: tuple[str, ...]
    ignore_filters: tuple[str, ...]
    also_emit: tuple[str, ...]


@dataclass(frozen=True)
class Offset:
    months: int = 0
    years: int = 0

    @property
    def total_months(self) -> int:
        return self.years * 12 + self.months


@dataclass(frozen=True)
class OutputSpec:
    key: str
    kind: OpName
    of: str | None = None
    offset: Offset | None = None
    trailing_months: int | None = None
    inclusive: bool = True
    fn: str | None = None
    left: str | None = None
    right: str | None = None
    cuts: tuple[str, ...] | None = None


@dataclass(frozen=True)
class KpiSpec:
    kpi_id: int | str
    version: int
    model_id: str
    time: TimeSpec
    dimensions: tuple[str, ...]
    base_measures: tuple[BaseMeasure, ...]
    cuts: tuple[CutSpec, ...]
    default_cut: str
    outputs: tuple[OutputSpec, ...]
    filter_map: dict[str, str] = field(default_factory=dict)
    row_set: Literal["span_union", "anchor_only"] = "span_union"


@dataclass(frozen=True)
class PhysicalSource:
    name: str
    alias: str


@dataclass(frozen=True)
class JoinSpec:
    left: str
    right: str
    on: tuple[str, ...]
    join_type: str = "left"


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    kind: Literal["physical", "sql"]
    required_aliases: tuple[str, ...]
    sources: tuple[PhysicalSource, ...] = ()
    joins: tuple[JoinSpec, ...] = ()
    sql: str | None = None
    output_schema: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimePlan:
    anchor: date
    span_start: date
    span_end_exclusive: date
    lookback_months: int
    claimed_filter_code: str


@dataclass(frozen=True)
class ExtractResult:
    frame: Any
    sql: str
    params: tuple[Any, ...]
