"""Compile and run the DuckDB extract.

What this file provides
    compile_extract — parameterized SQL (scan, time range, IN filters).
    extract — execute and return a Pandas frame plus the SQL string.

Where it is used
    orchestrator.compute (execute) and validate (compile only). Tests assert
    the selected month is not IN-filtered.

Capabilities
    - Physical models: read_parquet / delta_scan from context table_type.
    - SQL models: wrap CTE as a subquery ($alias_scan or $alias_path → ?).
    - Row-level model columns only. KPI YAML formulas and aggs run in Pandas.
    - Identifiers quoted; values bound as parameters.

When to use
    Change this for new scan types or join YAML. Do not put YoY/MTD or
    base_measures.sql math here — those belong in Pandas after retrieve.
    Do not duckdb.connect() for production — orchestrator passes the host session.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb

from kpi_engine.contracts import (
    BoundFilter,
    DatasetBinding,
    ExtractResult,
    KpiSpec,
    ModelSpec,
    TimePlan,
)
from kpi_engine.dates import duckdb_parse_time_sql
from kpi_engine.exceptions import BindError, KPIEngineError
from kpi_engine.identifiers import match_name, norm_name, quote_ident
from kpi_engine.runlog import log_sql, traced


NON_ADDITIVE = {"count_distinct", "median", "percentile", "first", "last"}
_PATH_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)_path")
_SCAN_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)_scan")


@traced
def extract(
    *,
    model: ModelSpec,
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    source_filters: tuple[BoundFilter, ...],
    plan: TimePlan | None,
    grain: tuple[str, ...],
    connection: duckdb.DuckDBPyConnection | None = None,
    row_level: bool = True,
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
    log_sql(sql, params, model=model.model_id, row_level=True)
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
    plan: TimePlan | None,
    grain: tuple[str, ...],
    row_level: bool = True,
    filter_columns: set[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build parameterized SELECT SQL of model columns. Never aggregates KPI YAML."""
    del row_level
    params: list[Any] = []
    from_sql = _from_clause(model, datasets, params)
    usable = source_filters
    time_col = kpi.time.column if kpi.time is not None else None
    if filter_columns is not None:
        usable = tuple(
            f
            for f in source_filters
            if match_name(f.column, filter_columns)
            or (time_col is not None and norm_name(f.column) == norm_name(time_col))
        )
    where_sql, where_params = _where_clause(
        usable, kpi, plan, model=model, datasets=datasets
    )
    params.extend(where_params)
    select_sql = _select_model_columns(kpi, grain, model=model, datasets=datasets)
    sql = f"SELECT {select_sql}\nFROM {from_sql}\nWHERE {where_sql}"
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
    plan: TimePlan | None,
    model: ModelSpec | None = None,
    datasets: dict[str, DatasetBinding] | None = None,
) -> tuple[str, list[Any]]:
    """Time range (>= span_start, < span_end) plus source IN filters. Empty IN is FALSE.

    Snapshot KPIs omit the range and keep only IN filters (or TRUE). SQL models
    apply IN on the wrapper around the whole CTE script (the final SELECT).
    """
    parts: list[str] = []
    params: list[Any] = []
    if kpi.time is not None and plan is not None:
        time_expr = _time_bucket_expr(kpi, model=model, datasets=datasets)
        parts.append(f"{time_expr} >= ?")
        params.append(plan.span_start)
        parts.append(f"{time_expr} < ?")
        params.append(plan.span_end_exclusive)
    prefix = _source_prefix(model)
    catalog = _physical_catalog(model, datasets)
    for item in source_filters:
        physical = match_name(item.column, catalog) or item.column
        col = f"{prefix}{quote_ident(physical)}"
        if not item.values:
            parts.append("FALSE")
            continue
        placeholders = ", ".join("?" for _ in item.values)
        parts.append(f"{col} IN ({placeholders})")
        params.extend(item.values)
    if not parts:
        return "TRUE", params
    return " AND ".join(parts), params


def _source_prefix(model: ModelSpec | None) -> str:
    """Qualify columns with the first physical source when YAML joins would be ambiguous."""
    if model is None or model.kind != "physical" or len(model.sources) < 2:
        return ""
    return f"{quote_ident(model.sources[0].alias)}."


def _select_model_columns(
    kpi: KpiSpec,
    grain: tuple[str, ...],
    model: ModelSpec | None = None,
    datasets: dict[str, DatasetBinding] | None = None,
) -> str:
    """SELECT time bucket, dims, and physical columns named by KPI YAML. No aggs."""
    from kpi_engine.catalog.ops_impl import input_columns

    time_col = kpi.time.column if kpi.time is not None else None
    parts: list[str] = []
    seen: set[str] = set()
    for col in grain:
        ident = quote_ident(col)
        if col == time_col:
            parts.append(
                f"{_time_bucket_expr(kpi, model=model, datasets=datasets)} AS {ident}"
            )
        else:
            parts.append(_select_physical(col, model, datasets, alias=col))
        seen.add(col)
    if time_col and any(m.agg in {"first", "last"} for m in kpi.base_measures):
        raw_time = _physical_ident(time_col, model, datasets)
        parsed = duckdb_parse_time_sql(
            raw_time, kpi.time.format if kpi.time is not None else None
        )
        parts.append(f"{parsed} AS {quote_ident('__event_time')}")
        seen.add("__event_time")
    for measure in kpi.base_measures:
        for col in input_columns(measure):
            if col in seen:
                continue
            parts.append(_select_physical(col, model, datasets, alias=col))
            seen.add(col)
    return ", ".join(parts)


def _time_bucket_expr(
    kpi: KpiSpec,
    model: ModelSpec | None = None,
    datasets: dict[str, DatasetBinding] | None = None,
) -> str:
    """SQL expression that truncates the time column to the KPI grain (DATE)."""
    if kpi.time is None:
        raise BindError("Cannot build a time bucket: this KPI has no time: block.")
    ident = _physical_ident(kpi.time.column, model, datasets)
    casted = duckdb_parse_time_sql(ident, kpi.time.format)
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


def _physical_catalog(
    model: ModelSpec | None, datasets: dict[str, DatasetBinding] | None
) -> list[str]:
    """Known physical column spellings from the model SELECT and context datasets."""
    names: list[str] = []
    if model is not None:
        names.extend(model.output_schema)
    if datasets:
        for dataset in datasets.values():
            names.extend(dataset.columns)
    return names


def _physical_ident(
    name: str, model: ModelSpec | None, datasets: dict[str, DatasetBinding] | None
) -> str:
    """Quoted physical column, using the table/CTE spelling when case differs."""
    physical = match_name(name, _physical_catalog(model, datasets)) or name
    return f"{_source_prefix(model)}{quote_ident(physical)}"


def _select_physical(
    name: str,
    model: ModelSpec | None,
    datasets: dict[str, DatasetBinding] | None,
    *,
    alias: str,
) -> str:
    """SELECT physical column, aliased to the KPI YAML name when they differ."""
    physical = match_name(name, _physical_catalog(model, datasets)) or name
    expr = f"{_source_prefix(model)}{quote_ident(physical)}"
    if physical != alias:
        return f"{expr} AS {quote_ident(alias)}"
    return expr


def _assert_no_month_in(sql: str, plan: TimePlan | None) -> None:
    """Guardrail: the anchor month must not appear as a lone IN list."""
    if plan is None:
        return
    compacted = " ".join(sql.upper().split())
    if " IN (" in compacted and str(plan.anchor) in sql:
        # Time range uses >= / < parameters, not the date literal. If the
        # anchor string is in SQL text, someone interpolated a month IN.
        raise KPIEngineError(
            "Generated SQL must not IN-filter the anchor month; use a range."
        )
