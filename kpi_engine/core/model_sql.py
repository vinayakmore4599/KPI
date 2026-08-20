"""Compile and run the DuckDB extract. Identifiers are quoted; values parameterized."""

from __future__ import annotations

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
from kpi_engine.exceptions import BindError, KPIEngineError
from kpi_engine.identifiers import quote_ident, require_ident


_ADDITIVE = {"sum", "count", "min", "max", "avg"}


def extract(
    *,
    model: ModelSpec,
    kpi: KpiSpec,
    datasets: dict[str, DatasetBinding],
    source_filters: tuple[BoundFilter, ...],
    plan: TimePlan,
    grain: tuple[str, ...],
    connection: duckdb.DuckDBPyConnection | None = None,
) -> ExtractResult:
    sql, params = compile_extract(
        model=model,
        kpi=kpi,
        datasets=datasets,
        source_filters=source_filters,
        plan=plan,
        grain=grain,
    )
    con = connection or duckdb.connect()
    own = connection is None
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
) -> tuple[str, tuple[Any, ...]]:
    params: list[Any] = []
    from_sql = _from_clause(model, datasets, params)
    where_sql, where_params = _where_clause(
        source_filters, kpi.time.column, plan
    )
    params.extend(where_params)
    select_sql = _select_clause(kpi, grain)
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
    if model.kind == "sql":
        if not model.sql:
            raise BindError("SQL model is missing sql.")
        inner = model.sql
        for alias, dataset in datasets.items():
            token = f"${alias}_path"
            if token in inner:
                inner = inner.replace(token, "?")
                params.append(dataset.path)
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


def _scan_fn(dataset: DatasetBinding) -> str:
    table_type = dataset.table_type.upper()
    path = dataset.path.lower()
    if table_type == "DELTA" and not path.endswith(".parquet"):
        return "delta_scan(?)"
    return "read_parquet(?)"


def _where_clause(
    source_filters: tuple[BoundFilter, ...],
    time_column: str,
    plan: TimePlan,
) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    time_expr = f"date_trunc('month', CAST({quote_ident(time_column)} AS DATE))"
    parts.append(f"{time_expr} >= ?")
    params.append(plan.span_start)
    parts.append(f"{time_expr} < ?")
    params.append(plan.span_end_exclusive)
    for item in source_filters:
        col = quote_ident(item.column)
        if not item.values:
            parts.append("FALSE")
            continue
        placeholders = ", ".join("?" for _ in item.values)
        parts.append(f"{col} IN ({placeholders})")
        params.extend(item.values)
    return " AND ".join(parts), params


def _select_clause(kpi: KpiSpec, grain: tuple[str, ...]) -> str:
    time_col = kpi.time.column
    select_parts: list[str] = []
    for col in grain:
        ident = quote_ident(col)
        if col == time_col:
            select_parts.append(
                f"date_trunc('month', CAST({ident} AS DATE)) AS {ident}"
            )
        else:
            select_parts.append(ident)
    for measure in kpi.base_measures:
        select_parts.extend(_measure_select(measure))
    return ", ".join(select_parts)


def _measure_select(measure: BaseMeasure) -> list[str]:
    if measure.agg not in _ADDITIVE:
        raise BindError(f"agg {measure.agg!r} cannot use the shared GROUP BY.")
    expr = quote_ident(require_ident(measure.sql, what="measure sql"))
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
