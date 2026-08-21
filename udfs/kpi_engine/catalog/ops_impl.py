"""Named Pandas functions for KPI YAML column math and measure composition.

What this file provides
    COLUMN_FNS — `base_measures.op` registry. Series in, Series out.
    MEASURE_FNS — `measures.fn` registry. Scalars in, one scalar out.
    apply_row_op — call a COLUMN_FNS entry with the declared columns.
    column_op_error / measure_fn_error — arity and parameter-name checks the
        binder runs against a signature, so YAML fails before any data is read.
    apply_where_mask — structured isin/eq/ne mask.
    apply_dimension_maps — CASE-style maps and optional grain trunc.
    apply_pandas_facts — compute every base_measure on the retrieved frame.
    eval_expr_series / eval_expr_scalar — nested + - * / after retrieve.
    call_measure_fn — call a MEASURE_FNS entry with one value per input measure.

Where it is used
    calc_engine and orchestrator after the DuckDB extract. binder validates
    YAML names against both registries at bind time.

When to use
    Register a function instead of editing a dispatch chain. Project-specific
    functions belong in kpi_engine.extensions.functions. Do not eval() YAML.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, NamedTuple

import pandas as pd

from kpi_engine.contracts import BaseMeasure, KpiSpec, MeasureWhere
from kpi_engine.exceptions import CatalogError
from kpi_engine.identifiers import (
    Expr,
    Group,
    Ident,
    Number,
    Unary,
    expression_columns,
    is_simple_ident,
    parse_expression,
)
from kpi_engine.runlog import traced

WHERE_OPS = {"in", "eq", "ne"}

ColumnFn = Callable[..., "pd.Series"]
MeasureFn = Callable[..., Any]

COLUMN_FNS: dict[str, ColumnFn] = {}
MEASURE_FNS: dict[str, MeasureFn] = {}

PASSTHROUGH_OPS = {"value", "identity"}


class FnMeta(NamedTuple):
    """How many arguments a registered function takes, and their bindable names."""

    min_args: int
    max_args: int | None
    params: tuple[str, ...]


_COLUMN_META: dict[str, FnMeta] = {}
_MEASURE_META: dict[str, FnMeta] = {}


def _read_signature(fn: Callable[..., Any], floor: int | None) -> FnMeta:
    """Derive arity and keyword-bindable names from a function's own signature.

    A `*args` function has no upper bound and no bindable names, so it takes any
    number of columns positionally. Everything else is bounded by its parameters.
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return FnMeta(max(1, floor or 1), None, ())
    required = 0
    optional = 0
    variadic = False
    names: list[str] = []
    for param in params:
        if param.kind is param.VAR_POSITIONAL:
            variadic = True
            continue
        if param.kind is param.VAR_KEYWORD:
            continue
        if param.kind is not param.POSITIONAL_ONLY:
            names.append(param.name)
        if param.default is param.empty:
            required += 1
        else:
            optional += 1
    low = max(1, required if floor is None else max(required, int(floor)))
    return FnMeta(low, None if variadic else required + optional, () if variadic else tuple(names))


def register_column_fn(
    name: str,
    fn: ColumnFn,
    *,
    min_columns: int | None = None,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register a `base_measures.op` function taking one numeric Series per column.

    Arity and parameter names come from the signature: `def fill(shipped, ordered)`
    accepts exactly two columns and can be called as `columns: {shipped: a, ordered: b}`,
    while `def total(*columns)` accepts any number. `min_columns` sets a floor for
    the `*columns` case, where the signature alone cannot say.
    """
    meta = _read_signature(fn, min_columns)
    for key in (name, *aliases):
        COLUMN_FNS[key] = fn
        _COLUMN_META[key] = meta


def unregister_column_fn(*names: str) -> None:
    """Remove column functions (used by tests to restore the allowlist)."""
    for name in names:
        COLUMN_FNS.pop(name, None)
        _COLUMN_META.pop(name, None)


def register_measure_fn(
    name: str,
    fn: MeasureFn,
    *,
    min_inputs: int | None = None,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register a `measures.fn` function taking one scalar per input measure.

    Arity and parameter names come from the signature, exactly as for column
    functions, so `def growth(current, previous)` can be fed by name.
    """
    meta = _read_signature(fn, min_inputs)
    for key in (name, *aliases):
        MEASURE_FNS[key] = fn
        _MEASURE_META[key] = meta


def unregister_measure_fn(*names: str) -> None:
    """Remove measure functions (used by tests to restore the allowlist)."""
    for name in names:
        MEASURE_FNS.pop(name, None)
        _MEASURE_META.pop(name, None)


def column_fn_meta(name: str) -> FnMeta:
    """Call shape of a registered column function."""
    return _COLUMN_META.get(name, FnMeta(1, None, ()))


def measure_fn_meta(name: str) -> FnMeta:
    """Call shape of a registered measure function."""
    return _MEASURE_META.get(name, FnMeta(1, None, ()))


def _arity_error(
    name: str, meta: FnMeta, count: int, *, unit: str, foldable: bool = False
) -> str | None:
    """Message when `count` arguments do not fit the registered signature."""
    hint = f" Parameters: {', '.join(meta.params)}." if meta.params else ""
    if count < meta.min_args:
        return f"{name!r} needs at least {meta.min_args} {unit} (got {count}).{hint}"
    if meta.max_args is None or count <= meta.max_args:
        return None
    if foldable and meta.max_args == 2:
        return None
    return f"{name!r} takes at most {meta.max_args} {unit} (got {count}).{hint}"


def _param_error(name: str, meta: FnMeta, given: tuple[str, ...], *, unit: str) -> str | None:
    """Message when a `{parameter: value}` mapping does not match the signature."""
    if not meta.params:
        return (
            f"{name!r} takes any number of {unit}, so it cannot be called with "
            f"parameter names. Use a plain list."
        )
    unknown = [p for p in given if p not in meta.params]
    if unknown:
        return (
            f"{name!r} has no parameter {unknown[0]!r}. "
            f"Parameters: {', '.join(meta.params)}."
        )
    return None


def column_op_error(name: str, count: int, params: tuple[str, ...] = ()) -> str | None:
    """Why `columns:` does not fit column function `name`, or None when it does."""
    meta = column_fn_meta(name)
    return _arity_error(name, meta, count, unit="columns") or (
        _param_error(name, meta, params, unit="columns") if params else None
    )


def measure_fn_error(name: str, count: int, params: tuple[str, ...] = ()) -> str | None:
    """Why `inputs:` does not fit measure function `name`, or None when it does."""
    meta = measure_fn_meta(name)
    return _arity_error(name, meta, count, unit="inputs", foldable=True) or (
        _param_error(name, meta, params, unit="inputs") if params else None
    )


def input_columns(measure: BaseMeasure) -> tuple[str, ...]:
    """Physical columns DuckDB must retrieve for this fact. Never a formula."""
    if measure.columns:
        return measure.columns
    source = measure.expr or measure.sql
    if not source:
        return ()
    if is_simple_ident(source):
        return (source.strip(),)
    return expression_columns(parse_expression(source, what="measure sql"))


def uses_pandas_row_op(measure: BaseMeasure) -> bool:
    """True when Pandas combines retrieved columns before agg (op / expr / sql math)."""
    if measure.row_op is not None and measure.row_op not in PASSTHROUGH_OPS:
        return True
    if measure.expr:
        return True
    if measure.sql and not is_simple_ident(measure.sql):
        return True
    return False


def eval_expr_series(node: Expr, frame: pd.DataFrame) -> pd.Series:
    """Evaluate an expression on one row of the extract at a time."""
    if isinstance(node, Ident):
        if node.name not in frame.columns:
            raise CatalogError(f"Expression names column {node.name!r}, which is not on the extract.")
        return pd.to_numeric(frame[node.name], errors="coerce")
    if isinstance(node, Number):
        return pd.Series(node.value, index=frame.index, dtype="float64")
    if isinstance(node, Unary):
        series = eval_expr_series(node.operand, frame)
        return series if node.op == "+" else -series
    if isinstance(node, Group):
        return eval_expr_series(node.inner, frame)
    left = eval_expr_series(node.left, frame)
    right = eval_expr_series(node.right, frame)
    if node.op == "+":
        return left + right
    if node.op == "-":
        return left - right
    if node.op == "*":
        return left * right
    return _series_div(left, right)


def eval_expr_scalar(node: Expr, values: dict[str, Any]) -> float | None:
    """Evaluate an expression over other measures' scalars."""
    if isinstance(node, Ident):
        if node.name not in values:
            raise CatalogError(f"Expression names measure {node.name!r}, which was not computed.")
        value = values[node.name]
        return None if value is None or (isinstance(value, float) and pd.isna(value)) else float(value)
    if isinstance(node, Number):
        return float(node.value)
    if isinstance(node, Unary):
        value = eval_expr_scalar(node.operand, values)
        if value is None:
            return None
        return value if node.op == "+" else -value
    if isinstance(node, Group):
        return eval_expr_scalar(node.inner, values)
    left = eval_expr_scalar(node.left, values)
    right = eval_expr_scalar(node.right, values)
    if left is None or right is None:
        return None
    if node.op == "+":
        return float(left + right)
    if node.op == "-":
        return float(left - right)
    if node.op == "*":
        return float(left * right)
    if right == 0:
        return None
    return float(left / right)


def _series_div(left: pd.Series, right: pd.Series) -> pd.Series:
    """Divide row by row, treating a zero denominator as null rather than inf."""
    return left / right.replace(0, pd.NA)


@traced
def apply_dimension_maps(frame: pd.DataFrame, kpi: KpiSpec) -> pd.DataFrame:
    """Rewrite dimension columns from `from` + `map` / `grain` after retrieve."""
    if frame.empty or not kpi.dimension_specs:
        return frame
    work = frame.copy()
    for spec in kpi.dimension_specs:
        src = spec.source or spec.name
        if src not in work.columns:
            continue
        series = work[src]
        if spec.grain:
            series = pd.to_datetime(series, errors="coerce").dt.to_period(
                {"day": "D", "month": "M", "quarter": "Q", "year": "Y"}[spec.grain]
            ).dt.start_time.dt.normalize()
        if spec.mapping:
            mapped = series.astype(str).map(spec.mapping)
            if spec.default is not None:
                mapped = mapped.fillna(spec.default)
            else:
                mapped = mapped.fillna(series.astype(str))
            series = mapped
        work[spec.name] = series
    return work


@traced
def apply_pandas_facts(frame: pd.DataFrame, kpi: KpiSpec) -> pd.DataFrame:
    """Compute every base measure from retrieved physical columns."""
    if frame.empty:
        return frame
    work = frame.copy()
    for measure in kpi.base_measures:
        work[measure.name] = _base_measure_series(work, measure)
    return work


def _base_measure_series(work: pd.DataFrame, measure: BaseMeasure) -> pd.Series:
    """One row-level series for a base measure (op, expr, sql column, or where)."""
    cols = input_columns(measure)
    missing = [c for c in cols if c not in work.columns]
    if missing:
        raise CatalogError(
            f"base_measures.{measure.name} needs columns {missing} on the extract."
        )
    if measure.row_op is not None and measure.row_op not in PASSTHROUGH_OPS:
        series = apply_row_op(work, cols, measure.row_op, measure.column_params)
    elif measure.expr:
        series = eval_expr_series(parse_expression(measure.expr, what="measure expr"), work)
    elif measure.sql and not is_simple_ident(measure.sql):
        series = eval_expr_series(parse_expression(measure.sql, what="measure sql"), work)
    elif cols:
        if measure.agg == "count_distinct":
            series = work[cols[0]]
        else:
            series = apply_row_op(work, cols[:1], "value")
    elif measure.name in work.columns:
        series = pd.to_numeric(work[measure.name], errors="coerce")
    else:
        raise CatalogError(f"base_measures.{measure.name} has no columns to compute.")
    if measure.where is not None:
        series = series.where(apply_where_mask(work, measure.where))
    return series


def apply_where_mask(frame: pd.DataFrame, spec: MeasureWhere) -> pd.Series:
    """Boolean mask for where.column op values."""
    if spec.column not in frame.columns:
        raise CatalogError(f"where.column {spec.column!r} is not on the extract.")
    col = frame[spec.column]
    op = spec.op.lower()
    if op == "in":
        return col.isin(list(spec.values))
    if op == "eq":
        value = spec.values[0] if spec.values else None
        return col == value
    if op == "ne":
        value = spec.values[0] if spec.values else None
        return col != value
    raise CatalogError(f"Unknown where.op {spec.op!r}. Use in, eq, or ne.")


def apply_row_op(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
    op: str | None,
    params: tuple[str, ...] = (),
) -> pd.Series:
    """Call the registered column function with the declared columns as Series.

    `params` are the keys of a `{parameter: column}` mapping, positionally aligned
    with `columns`; when present the function is called by keyword instead of order.
    """
    name = op or "value"
    fn = COLUMN_FNS.get(name)
    if fn is None:
        raise CatalogError(
            f"Unknown column op {name!r}. Registered: {sorted(COLUMN_FNS)}. "
            "Register it with kpi_engine.extensions.functions.register_column_fn."
        )
    if not columns:
        raise CatalogError("Column op needs `columns:` (or sql:).")
    problem = column_op_error(name, len(columns), params)
    if problem:
        raise CatalogError(f"Column op {problem}")
    args = [pd.to_numeric(frame[c], errors="coerce") for c in columns]
    result = fn(**dict(zip(params, args))) if params else fn(*args)
    if not isinstance(result, pd.Series):
        raise CatalogError(
            f"Column op {name!r} must return a pandas Series (got {type(result).__name__})."
        )
    if len(result) != len(frame):
        raise CatalogError(
            f"Column op {name!r} returned {len(result)} values for {len(frame)} rows."
        )
    return result.reindex(frame.index) if not result.index.equals(frame.index) else result


def _fold(step: Callable[[Any, Any], Any], args: tuple[Any, ...]) -> Any:
    """Apply a two-argument step left to right across every operand."""
    result = args[0]
    for item in args[1:]:
        result = step(result, item)
    return result


def _side_by_side(columns: tuple[pd.Series, ...]) -> pd.DataFrame:
    """Line the operands up as columns so a row-wise reducer can run across them."""
    return pd.concat(columns, axis=1)


def _value(column: pd.Series) -> pd.Series:
    """Pass one column through unchanged."""
    return column


def _divide_columns(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide row by row, treating a zero denominator as null rather than inf."""
    return numerator / denominator.replace(0, pd.NA)


def _percent_of_columns(part: pd.Series, whole: pd.Series) -> pd.Series:
    """Share of `whole`, scaled to 0-100."""
    return _divide_columns(part, whole) * 100


register_column_fn("value", _value, aliases=("identity",))
register_column_fn("abs", lambda column: column.abs())
register_column_fn(
    "sum", lambda *columns: _fold(lambda a, b: a + b, columns), min_columns=2, aliases=("add",)
)
register_column_fn(
    "subtract", lambda *columns: _fold(lambda a, b: a - b, columns), min_columns=2, aliases=("sub",)
)
register_column_fn(
    "multiply",
    lambda *columns: _fold(lambda a, b: a * b, columns),
    min_columns=2,
    aliases=("mul", "product"),
)
register_column_fn("divide", _divide_columns, aliases=("div", "ratio"))
register_column_fn("percent_of", _percent_of_columns, aliases=("share",))
register_column_fn("min", lambda *columns: _side_by_side(columns).min(axis=1), min_columns=2)
register_column_fn("max", lambda *columns: _side_by_side(columns).max(axis=1), min_columns=2)
register_column_fn(
    "avg", lambda *columns: _side_by_side(columns).mean(axis=1), min_columns=2, aliases=("mean",)
)
register_column_fn(
    "coalesce", lambda *columns: _fold(lambda a, b: a.fillna(b), columns), min_columns=2
)


def call_measure_fn(fn: str, values: list[Any], params: tuple[str, ...] = ()) -> Any:
    """Call a registered measure function with one argument per input measure.

    A strictly two-argument function given more than two values is folded left to
    right, which is what `arithmetic` with a three-name `of:` list has always meant.
    """
    step = MEASURE_FNS.get(fn)
    if step is None:
        raise CatalogError(
            f"Unknown measure fn {fn!r}. Registered: {sorted(MEASURE_FNS)}. "
            "Register it with kpi_engine.extensions.functions.register_measure_fn."
        )
    if not values:
        return None
    problem = measure_fn_error(fn, len(values), params)
    if problem:
        raise CatalogError(f"Measure fn {problem}")
    if params:
        return step(**dict(zip(params, values)))
    if measure_fn_meta(fn).max_args == 2 and len(values) > 2:
        return _fold(step, tuple(values))
    return step(*values)


def _growth_pct(current: Any, previous: Any) -> float | None:
    """Relative change from `previous` to `current`. A zero or null base yields null."""
    if current is None or previous in (None, 0):
        return None
    return float((current - previous) / previous)


def _divide(numerator: Any, denominator: Any) -> float | None:
    """Ratio. A zero or null denominator yields null, never inf."""
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator / denominator)


def _percent(part: Any, whole: Any) -> float | None:
    """Share of `whole`, scaled to 0-100."""
    value = _divide(part, whole)
    return None if value is None else float(value * 100)


def _numeric_fold(op: Callable[[Any, Any], Any]) -> MeasureFn:
    """Wrap a two-scalar operation as a variadic fold where any null yields null."""

    def step(*values: Any) -> float | None:
        """Apply the operation left to right unless an operand is null."""
        if any(value is None for value in values):
            return None
        return float(_fold(op, values))

    return step


def _numeric_pick(choose: Callable[[list[Any]], Any]) -> MeasureFn:
    """Wrap a reducer that ignores nulls and yields null only when nothing is left."""

    def step(*values: Any) -> float | None:
        """Reduce the non-null operands."""
        present = [value for value in values if value is not None]
        return None if not present else float(choose(present))

    return step


register_measure_fn("growth_pct", _growth_pct, aliases=("yoy", "mom", "percent_change"))
register_measure_fn("divide", _divide, aliases=("div", "ratio"))
register_measure_fn("percent", _percent, aliases=("percent_of", "share"))
register_measure_fn("sum", _numeric_fold(lambda a, b: a + b), min_inputs=2, aliases=("add",))
register_measure_fn("subtract", _numeric_fold(lambda a, b: a - b), min_inputs=2, aliases=("sub",))
register_measure_fn(
    "multiply", _numeric_fold(lambda a, b: a * b), min_inputs=2, aliases=("mul", "product")
)
register_measure_fn("min", _numeric_pick(min), min_inputs=2)
register_measure_fn("max", _numeric_pick(max), min_inputs=2)
register_measure_fn(
    "avg", _numeric_pick(lambda vs: sum(vs) / len(vs)), min_inputs=2, aliases=("mean",)
)


def _sum_or_null(series: pd.Series) -> float:
    """Sum that stays null when every value is null (pandas default would be 0)."""
    return series.sum(min_count=1)


def pandas_group_keys(kpi: KpiSpec, grain: tuple[str, ...]) -> list[str]:
    """Grouping columns after dimension maps (mapped name, not `from`)."""
    time_col = kpi.time.column if kpi.time is not None else None
    rename = {spec.source: spec.name for spec in kpi.dimension_specs if spec.source}
    keys: list[str] = []
    for col in grain:
        if col == time_col:
            continue
        keys.append(rename.get(col, col))
    return keys


@traced
def collapse_pandas_detail(
    detail: pd.DataFrame,
    kpi: KpiSpec,
    grain: tuple[str, ...],
) -> pd.DataFrame:
    """Compute KPI YAML facts per row, then fold additive aggs to the extract grain."""
    from kpi_engine.core.model_sql import NON_ADDITIVE

    if detail is None or detail.empty:
        return pd.DataFrame()
    work = apply_dimension_maps(detail, kpi)
    work = apply_pandas_facts(work, kpi)
    time_col = kpi.time.column if kpi.time is not None else None
    keys = list(pandas_group_keys(kpi, grain))
    if time_col is not None and time_col in work.columns:
        keys = [time_col, *keys]
    keys = [c for c in keys if c in work.columns]
    aggs: dict[str, str] = {}
    foldable = {"sum", "avg", "min", "max", "count", "first", "last"}
    for measure in kpi.base_measures:
        if measure.name not in work.columns:
            continue
        if measure.agg in NON_ADDITIVE:
            continue
        if measure.agg not in foldable:
            raise CatalogError(
                f"base_measures.{measure.name} agg={measure.agg!r} cannot fold a Pandas "
                "column. Use sum, avg, count, min, max, first, or last."
            )
        if measure.agg == "avg":
            work[f"{measure.name}__sum"] = pd.to_numeric(work[measure.name], errors="coerce")
            work[f"{measure.name}__count"] = work[measure.name].notna().astype("int64")
            aggs[f"{measure.name}__sum"] = _sum_or_null
            aggs[f"{measure.name}__count"] = "sum"
            continue
        if measure.agg == "sum":
            aggs[measure.name] = _sum_or_null
            continue
        aggs[measure.name] = measure.agg
    if not aggs:
        return pd.DataFrame()
    if time_col is not None and time_col in work.columns:
        work = work.sort_values(time_col)
    if not keys:
        out = work[list(aggs)].agg(aggs)
        return out.to_frame().T if isinstance(out, pd.Series) else out.reset_index(drop=True)
    grouped = work.groupby(keys, dropna=False, as_index=False).agg(aggs)
    return grouped
