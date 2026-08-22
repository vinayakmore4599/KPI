"""Compile and run the DuckDB extract.

What this file provides
    compile_extract — parameterized SQL (scan, time range, extract filters).
    extract — execute and return a Pandas frame plus the SQL string.

Where it is used
    orchestrator.compute (execute) and validate (compile only). Tests assert
    the selected month is not IN-filtered.

Capabilities
    - Physical models: read_parquet / delta_scan from context table_type.
    - SQL models: wrap CTE as a subquery ($alias_scan or $alias_path → ?).
    - Row-level model columns only. KPI YAML formulas and aggs run in Pandas.
    - Identifiers quoted; values bound as parameters.
    - Extract filters compile to the same operators as Pandas calc/result.
    - Extract `filters:` compile to the same operators as Pandas calc/result.

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
from kpi_engine.core.filter_ops import sql_predicate
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
    select_sql = _select_model_columns(
        kpi,
        grain,
        model=model,
        datasets=datasets,
        source_filters=usable,
    )
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
    """Time range (>= span_start, < span_end) plus extract filters. Empty IN is FALSE.

    Snapshot KPIs omit the range and keep only extract filters (or TRUE). SQL models
    apply filters on the wrapper around the whole CTE script (the final SELECT).
    """
    parts: list[str] = []
    params: list[Any] = []
    if kpi.time is not None and plan is not None:
        time_expr = _time_bucket_expr(kpi, model=model, datasets=datasets)
        parts.append(f"{time_expr} >= ?")
        params.append(plan.span_start)
        parts.append(f"{time_expr} < ?")
        params.append(plan.span_end_exclusive)
    for item in source_filters:
        col = _qualified_column(item.column, model, datasets)
        fragment, bound = sql_predicate(col, item.op, item.values)
        parts.append(fragment)
        params.extend(bound)
    if not parts:
        return "TRUE", params
    return " AND ".join(parts), params


def _column_source(
    name: str, model: ModelSpec | None, datasets: dict[str, DatasetBinding] | None
) -> str | None:
    """Owning physical alias for a column. Join keys stay on the left/fact source."""
    if model is None or model.kind != "physical" or len(model.sources) < 2:
        return None
    fact_alias = model.sources[0].alias
    for join in model.joins:
        if match_name(name, join.on):
            return fact_alias
    owners: list[str] = []
    for source in model.sources:
        dataset = None
        if datasets:
            dataset = datasets.get(source.alias) or datasets.get(source.name)
        if dataset is not None and match_name(name, dataset.columns):
            owners.append(source.alias)
    if not owners:
        return fact_alias
    if fact_alias in owners:
        return fact_alias
    return owners[0]


def _qualified_column(
    name: str, model: ModelSpec | None, datasets: dict[str, DatasetBinding] | None
) -> str:
    """Quoted column, prefixed with the owning join alias when the model has joins."""
    physical = match_name(name, _physical_catalog(model, datasets)) or name
    source = _column_source(name, model, datasets)
    if source:
        return f"{quote_ident(source)}.{quote_ident(physical)}"
    return quote_ident(physical)


def _select_model_columns(
    kpi: KpiSpec,
    grain: tuple[str, ...],
    model: ModelSpec | None = None,
    datasets: dict[str, DatasetBinding] | None = None,
    source_filters: tuple[BoundFilter, ...] = (),
) -> str:
    """SELECT context fact columns, grain, needed facts, and used join columns."""
    from kpi_engine.catalog.ops_impl import input_columns

    _assert_facts_on_context(kpi, model=model, datasets=datasets)
    time_col = kpi.time.column if kpi.time is not None else None
    parts: list[str] = []
    seen: set[str] = set()

    def add(name: str, *, time: bool = False) -> None:
        """Append one projection unless this folded name is already selected."""
        key = norm_name(name)
        if key in seen:
            return
        seen.add(key)
        ident = quote_ident(name)
        if time:
            parts.append(
                f"{_time_bucket_expr(kpi, model=model, datasets=datasets)} AS {ident}"
            )
            return
        parts.append(_select_physical(name, model, datasets, alias=name))

    for col in grain:
        add(col, time=bool(time_col and norm_name(col) == norm_name(time_col)))
    for measure in kpi.base_measures:
        for col in input_columns(measure):
            add(col)
        if measure.where is not None:
            add(measure.where.column)
    if time_col and any(m.agg in {"first", "last"} for m in kpi.base_measures):
        raw_time = _physical_ident(time_col, model, datasets)
        parsed = duckdb_parse_time_sql(
            raw_time, kpi.time.format if kpi.time is not None else None
        )
        if "__event_time" not in seen:
            parts.append(f"{parsed} AS {quote_ident('__event_time')}")
            seen.add("__event_time")
    for col in _fact_dataset_columns(kpi, datasets):
        if time_col and norm_name(col) == norm_name(time_col):
            continue
        add(col)
    for col in _used_join_columns(kpi, grain, source_filters, model, datasets):
        add(col)
    if not parts:
        raise BindError(
            "Extract SELECT is empty. List columns on context.datasets[].columns."
        )
    return ", ".join(parts)


def _fact_dataset_columns(
    kpi: KpiSpec, datasets: dict[str, DatasetBinding] | None
) -> list[str]:
    """Context columns from datasets that carry a needed fact or the time column.

    Dim-only aliases (regions.eligible, suppliers.active) stay off the retrieve
    so a SQL wrapper or join does not project columns the extract does not have.
    """
    from kpi_engine.catalog.ops_impl import input_columns

    if not datasets:
        return []
    needed = {norm_name(col) for measure in kpi.base_measures for col in input_columns(measure)}
    if kpi.time is not None:
        needed.add(norm_name(kpi.time.column))
    cols: list[str] = []
    seen: set[str] = set()
    for dataset in datasets.values():
        names = {norm_name(col) for col in dataset.columns}
        if needed and not (names & needed):
            continue
        for col in dataset.columns:
            key = norm_name(col)
            if key in seen:
                continue
            seen.add(key)
            cols.append(col)
    return cols


def _used_column_names(
    kpi: KpiSpec,
    grain: tuple[str, ...],
    source_filters: tuple[BoundFilter, ...],
) -> set[str]:
    """Folded names this request needs: grain, facts, and DuckDB extract filters."""
    from kpi_engine.catalog.ops_impl import input_columns

    used = {norm_name(col) for col in grain}
    for measure in kpi.base_measures:
        used.update(norm_name(col) for col in input_columns(measure))
        if measure.where is not None:
            used.add(norm_name(measure.where.column))
    for item in source_filters:
        used.add(norm_name(item.column))
    return used


def _joined_dataset_aliases(model: ModelSpec) -> list[str]:
    """Right-hand aliases declared on physical YAML joins."""
    aliases: list[str] = []
    seen: set[str] = set()
    for join in model.joins:
        key = norm_name(join.right)
        if key in seen:
            continue
        seen.add(key)
        aliases.append(join.right)
    return aliases


def _used_join_columns(
    kpi: KpiSpec,
    grain: tuple[str, ...],
    source_filters: tuple[BoundFilter, ...],
    model: ModelSpec | None,
    datasets: dict[str, DatasetBinding] | None,
) -> list[str]:
    """Context columns on joined aliases that grain, filters, or facts actually use."""
    if not datasets:
        return []
    used = _used_column_names(kpi, grain, source_filters)
    from kpi_engine.catalog.ops_impl import input_columns

    fact_needed = {
        norm_name(col) for measure in kpi.base_measures for col in input_columns(measure)
    }
    if kpi.time is not None:
        fact_needed.add(norm_name(kpi.time.column))
    if model is not None and model.kind == "physical" and model.joins:
        joined = _joined_dataset_aliases(model)
    else:
        joined = [
            alias
            for alias, dataset in datasets.items()
            if not ({norm_name(c) for c in dataset.columns} & fact_needed)
        ]
    cols: list[str] = []
    seen: set[str] = set()
    for alias in joined:
        dataset = datasets.get(alias)
        if dataset is None:
            dataset = next(
                (ds for key, ds in datasets.items() if norm_name(key) == norm_name(alias)),
                None,
            )
        if dataset is None:
            continue
        for col in dataset.columns:
            key = norm_name(col)
            if key not in used or key in seen:
                continue
            seen.add(key)
            cols.append(col)
    return cols


def _assert_facts_on_context(
    kpi: KpiSpec,
    model: ModelSpec | None,
    datasets: dict[str, DatasetBinding] | None,
) -> None:
    """Needed fact columns must be listed on context or output_schema when either is set."""
    from kpi_engine.catalog.ops_impl import input_columns

    catalog = _physical_catalog(model, datasets)
    host_listed = any(ds.columns for ds in (datasets or {}).values())
    schema_listed = bool(model is not None and model.output_schema)
    if not host_listed and not schema_listed:
        return
    allowed = {norm_name(col) for col in catalog}
    aliases = sorted({ds.alias for ds in (datasets or {}).values()}) or ["(none)"]
    for measure in kpi.base_measures:
        for col in input_columns(measure):
            if norm_name(col) in allowed:
                continue
            raise BindError(
                f"base_measures.{measure.name} needs column {col!r} on "
                f"context.datasets[].columns or model.output_schema "
                f"(datasets {aliases})."
            )


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
    return _qualified_column(name, model, datasets)


def _select_physical(
    name: str,
    model: ModelSpec | None,
    datasets: dict[str, DatasetBinding] | None,
    *,
    alias: str,
) -> str:
    """SELECT physical column, aliased to the KPI YAML name when they differ."""
    physical = match_name(name, _physical_catalog(model, datasets)) or name
    expr = _qualified_column(name, model, datasets)
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
