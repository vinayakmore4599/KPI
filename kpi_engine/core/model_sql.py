"""Compile and run the DuckDB extract.

What this file provides
    compile_extract — parameterized SQL (scan, time range, IN filters, GROUP BY).
    extract — execute and return a Pandas frame plus the SQL string.

Where it is used
    orchestrator.compute (execute) and validate (compile only). Tests assert
    the selected month is not IN-filtered.

Capabilities
    - Physical models: read_parquet / delta_scan from context table_type.
    - SQL models: wrap CTE as a subquery ($alias_scan or $alias_path → ?).
    - Additive aggs in DuckDB (sum/count/min/max; avg as sum+count).
    - Identifiers quoted; values bound as parameters.

When to use
    Change this for new scan types or join YAML. Do not put YoY/MTD here —
    those belong in calc_engine after the monthly extract.
    Do not duckdb.connect() for production — orchestrator passes the host session.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from kpi_engine.contracts import (
    BaseMeasure,
    BoundFilter,
    DatasetBinding,
    ExtractResult,
    KpiSpec,
    ModelSpec,
    TimePlan,
)
from kpi_engine.exceptions import BindError, FilterError, KPIEngineError
from kpi_engine.identifiers import quote_ident, require_ident
from kpi_engine.runlog import log_sql, traced


_ADDITIVE = {"sum", "count", "min", "max", "avg"}
NON_ADDITIVE = {"count_distinct", "median", "percentile"}
_NON_ADDITIVE = NON_ADDITIVE
_PATH_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)_path")
_SCAN_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)_scan")


@traced
def extract(
    *,
    model: ModelSpec,
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    source_filters: tuple[BoundFilter, ...],
    plan: TimePlan,
    grain: tuple[str, ...],
    connection: duckdb.DuckDBPyConnection | None = None,
    row_level: bool = False,
    filter_columns: set[str] | None = None,
) -> ExtractResult:
    """Compile and execute the DuckDB extract. Opens a connection if none is passed."""
    sql, params = compile_extract(
        model=model,
        kpi=kpi,
        datasets=datasets,
        source_filters=source_filters,
        plan=plan,
        grain=grain,
        row_level=row_level,
        filter_columns=filter_columns,
    )
    log_sql(sql, params, model=model.model_id, row_level=row_level)
    con = connection
    own = False
    if con is None:
        con = duckdb.connect()
        own = True
    try:
        frame = con.execute(sql, list(params)).fetchdf()
    except Exception as exc:  # noqa: BLE001 — surface engine error, keep DuckDB details
        raise KPIEngineError(f"DuckDB extract failed: {exc}") from exc
    finally:
        if own:
            con.close()
    return ExtractResult(frame=frame, sql=sql, params=params)


def compile_extract(
    *,
    model: ModelSpec,
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    source_filters: tuple[BoundFilter, ...],
    plan: TimePlan,
    grain: tuple[str, ...],
    row_level: bool = False,
    filter_columns: set[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build parameterized SELECT SQL. GROUP BY unless row_level (non-additive detail)."""
    params: list[Any] = []
    from_sql = _from_clause(model, datasets, params)
    usable = source_filters
    if filter_columns is not None:
        usable = tuple(
            f for f in source_filters if f.column in filter_columns or f.column == kpi.time.column
        )
    where_sql, where_params = _where_clause(
        usable, kpi, plan, model=model
    )
    params.extend(where_params)
    if row_level:
        select_sql = _select_row_level(kpi, grain, model=model)
        sql = f"SELECT {select_sql}\nFROM {from_sql}\nWHERE {where_sql}"
    else:
        select_sql = _select_clause(kpi, grain, model=model)
        group_sql = ", ".join(str(i) for i in range(1, len(grain) + 1))
        sql = (
            f"SELECT {select_sql}\n"
            f"FROM {from_sql}\n"
            f"WHERE {where_sql}\n"
            f"GROUP BY {group_sql}"
        )
    _assert_no_month_in(sql, plan)
    return sql, tuple(params)


def _from_clause(
    model: ModelSpec,
    datasets: dict[str, DatasetBinding],
    params: list[Any],
) -> str:
    """FROM clause: parquet/delta scan or a wrapped SQL model. Paths go into params."""
    if model.kind == "sql":
        if not model.sql:
            raise BindError("SQL model is missing sql.")
        inner = _substitute_source_tokens(model.sql, datasets, params)
        return f"(\n{inner}\n) AS {quote_ident(model.model_id)}"

    if not model.sources:
        alias = model.required_aliases[0]
        dataset = datasets[alias]
        params.append(dataset.path)
        return f"{_scan_fn(dataset)} AS {quote_ident(alias)}"

    parts: list[str] = []
    for i, source in enumerate(model.sources):
        dataset = datasets.get(source.alias) or datasets.get(source.name)
        if dataset is None:
            raise BindError(f"No dataset bound for source {source.alias}.")
        params.append(dataset.path)
        scan = f"{_scan_fn(dataset)} AS {quote_ident(source.alias)}"
        if i == 0:
            parts.append(scan)
            continue
        join = next((j for j in model.joins if j.right in {source.alias, source.name}), None)
        if join is None:
            raise BindError(f"Source {source.alias} has no join.")
        on = " AND ".join(
            f"{quote_ident(join.left)}.{quote_ident(col)} = "
            f"{quote_ident(join.right)}.{quote_ident(col)}"
            for col in join.on
        )
        join_kw = join.join_type.upper()
        if join_kw not in {"LEFT", "INNER", "RIGHT"}:
            raise BindError(f"Unsupported join type {join.join_type!r}.")
        parts.append(f"{join_kw} JOIN {scan} ON {on}")
    return "\n".join(parts)


def _substitute_source_tokens(
    sql: str, datasets: dict[str, DatasetBinding], params: list[Any]
) -> str:
    """Replace $alias_scan then $alias_path left-to-right so ? params match CTE order."""
    by_alias = {alias.lower(): dataset for alias, dataset in datasets.items()}

    def scan_repl(match: re.Match[str]) -> str:
        """Bind $alias_scan to delta_scan(?) or read_parquet(?) from table_type."""
        dataset = _dataset_for_token(match.group(1), by_alias, datasets, token="scan")
        params.append(dataset.path)
        return _scan_fn(dataset)

    def path_repl(match: re.Match[str]) -> str:
        """Bind one $alias_path occurrence to a positional path parameter."""
        dataset = _dataset_for_token(match.group(1), by_alias, datasets, token="path")
        params.append(dataset.path)
        return "?"

    sql = _SCAN_TOKEN.sub(scan_repl, sql)
    return _PATH_TOKEN.sub(path_repl, sql)


def _dataset_for_token(
    alias: str,
    by_alias: dict[str, DatasetBinding],
    datasets: dict[str, DatasetBinding],
    *,
    token: str,
) -> DatasetBinding:
    """Resolve $alias_scan / $alias_path to a bound dataset."""
    dataset = by_alias.get(alias.lower())
    if dataset is None:
        raise BindError(
            f"SQL model references ${alias}_{token} but alias {alias!r} is not bound. "
            f"Bound: {sorted(datasets)}."
        )
    return dataset


def _scan_fn(dataset: DatasetBinding) -> str:
    """DuckDB scan function: delta_scan for Delta, read_parquet otherwise (including tests)."""
    table_type = dataset.table_type.upper()
    path = dataset.path.lower()
    if table_type == "DELTA" and not path.endswith(".parquet"):
        return "delta_scan(?)"
    return "read_parquet(?)"


def _where_clause(
    source_filters: tuple[BoundFilter, ...],
    kpi: KpiSpec,
    plan: TimePlan,
    model: ModelSpec | None = None,
) -> tuple[str, list[Any]]:
    """Time range (>= span_start, < span_end) plus source IN filters. Empty IN is FALSE."""
    selectable = set(model.output_schema) if model and model.kind == "sql" and model.output_schema else None
    parts: list[str] = []
    params: list[Any] = []
    time_expr = _time_bucket_expr(kpi, model=model)
    parts.append(f"{time_expr} >= ?")
    params.append(plan.span_start)
    parts.append(f"{time_expr} < ?")
    params.append(plan.span_end_exclusive)
    prefix = _source_prefix(model)
    for item in source_filters:
        if selectable is not None and item.column not in selectable and item.column != kpi.time.column:
            raise FilterError(
                f"Filter {item.code!r} maps to {item.column!r}, which is not in the "
                "model SELECT (output_schema). Expose that column in the CTE to filter it in DuckDB."
            )
        col = f"{prefix}{quote_ident(item.column)}"
        if not item.values:
            parts.append("FALSE")
            continue
        placeholders = ", ".join("?" for _ in item.values)
        parts.append(f"{col} IN ({placeholders})")
        params.extend(item.values)
    return " AND ".join(parts), params


def _source_prefix(model: ModelSpec | None) -> str:
    """Qualify columns with the first physical source when YAML joins would be ambiguous."""
    if model is None or model.kind != "physical" or len(model.sources) < 2:
        return ""
    return f"{quote_ident(model.sources[0].alias)}."


def _select_clause(kpi: KpiSpec, grain: tuple[str, ...], model: ModelSpec | None = None) -> str:
    """SELECT grain columns (time truncated to grain) plus aggregated additive measures."""
    time_col = kpi.time.column
    prefix = _source_prefix(model)
    select_parts: list[str] = []
    for col in grain:
        ident = quote_ident(col)
        if col == time_col:
            select_parts.append(f"{_time_bucket_expr(kpi, model=model)} AS {ident}")
        else:
            select_parts.append(f"{prefix}{ident}")
    for measure in kpi.base_measures:
        if measure.agg in _NON_ADDITIVE:
            continue
        select_parts.extend(_measure_select(measure, prefix=prefix))
    return ", ".join(select_parts)


def _select_row_level(
    kpi: KpiSpec, grain: tuple[str, ...], model: ModelSpec | None = None
) -> str:
    """SELECT time bucket, dims, and raw columns for non-additive aggs (no GROUP BY)."""
    time_col = kpi.time.column
    prefix = _source_prefix(model)
    parts: list[str] = []
    seen: set[str] = set()
    for col in grain:
        ident = quote_ident(col)
        if col == time_col:
            parts.append(f"{_time_bucket_expr(kpi, model=model)} AS {ident}")
        else:
            parts.append(f"{prefix}{ident}")
        seen.add(col)
    for measure in kpi.base_measures:
        if measure.agg not in _NON_ADDITIVE:
            continue
        if measure.sql in seen:
            continue
        parts.append(f"{prefix}{quote_ident(require_ident(measure.sql, what='measure sql'))}")
        seen.add(measure.sql)
    return ", ".join(parts)


def _time_bucket_expr(kpi: KpiSpec, model: ModelSpec | None = None) -> str:
    """SQL expression that truncates the time column to the KPI grain (DATE)."""
    ident = f"{_source_prefix(model)}{quote_ident(kpi.time.column)}"
    casted = f"CAST({ident} AS DATE)"
    grain = kpi.time.grain
    if kpi.time.calendar == "fiscal" and grain in {"quarter", "year"}:
        shift = kpi.time.fiscal_start_month - 1
        if grain == "year":
            return (
                f"CAST(date_trunc('year', {casted} - INTERVAL {shift} MONTH) "
                f"+ INTERVAL {shift} MONTH AS DATE)"
            )
        return (
            f"CAST(date_trunc('quarter', {casted} - INTERVAL {shift} MONTH) "
            f"+ INTERVAL {shift} MONTH AS DATE)"
        )
    unit = {"day": "day", "month": "month", "quarter": "quarter", "year": "year"}[grain]
    return f"CAST(date_trunc('{unit}', {casted}) AS DATE)"


def _measure_select(measure: BaseMeasure, prefix: str = "") -> list[str]:
    """SQL fragments for one additive agg. avg is carried as SUM and COUNT."""
    if measure.agg not in _ADDITIVE:
        raise BindError(f"agg {measure.agg!r} cannot use the shared GROUP BY.")
    expr = f"{prefix}{quote_ident(require_ident(measure.sql, what='measure sql'))}"
    name = quote_ident(measure.name)
    if measure.agg == "sum":
        return [f"SUM({expr}) AS {name}"]
    if measure.agg == "count":
        return [f"COUNT({expr}) AS {name}"]
    if measure.agg == "min":
        return [f"MIN({expr}) AS {name}"]
    if measure.agg == "max":
        return [f"MAX({expr}) AS {name}"]
    if measure.agg == "avg":
        sum_name = quote_ident(f"{measure.name}__sum")
        cnt_name = quote_ident(f"{measure.name}__count")
        return [f"SUM({expr}) AS {sum_name}", f"COUNT({expr}) AS {cnt_name}"]
    raise BindError(f"Unsupported agg {measure.agg!r}.")


def _assert_no_month_in(sql: str, plan: TimePlan) -> None:
    """Guardrail: the anchor month must not appear as a lone IN list."""
    compacted = " ".join(sql.upper().split())
    if " IN (" in compacted and str(plan.anchor) in sql:
        # Time range uses >= / < parameters, not the date literal. If the
        # anchor string is in SQL text, someone interpolated a month IN.
        raise KPIEngineError(
            "Generated SQL must not IN-filter the anchor month; use a range."
        )
