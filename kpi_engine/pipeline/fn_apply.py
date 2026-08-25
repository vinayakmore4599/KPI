"""In-memory function maps and Pandas apply helpers (engine, not a capability).

What this file provides
    COLUMN_FNS / MEASURE_FNS — filled by the registry loader.
    apply_row_op, apply_pandas_facts, fold_extract_columns, call_measure_fn.

When to use
    Engine retrieve/calc. New function *bodies* go in capabilities/functions/
    plus registries/functions/. Do not add kind branches here.
"""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

import pandas as pd

from kpi_engine.contracts import BaseMeasure, KpiSpec, MeasureWhere
from kpi_engine.exceptions import CatalogError
from kpi_engine.core.filter_ops import pandas_mask
from kpi_engine.identifiers import (
    Binary,
    BoolOp,
    Call,
    Case,
    Compare,
    Expr,
    Group,
    Ident,
    In,
    IsNull,
    ListLit,
    Not,
    Null,
    Number,
    String,
    Unary,
    expression_columns,
    is_simple_ident,
    match_name,
    norm_name,
    parse_expression,
)
from kpi_engine.runlog import traced

WHERE_OPS = {"in", "eq", "ne", "gt", "gte", "lt", "lte", "between"}
WHERE_OPS_HELP = "in, eq, ne, gt, gte, lt, lte, or between"
NUMERIC_WHERE_OPS = frozenset({"gt", "gte", "lt", "lte", "between"})
COUNT_AGGS = frozenset({"count", "count_distinct"})

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
_EXPR_ENV: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "kpi_expr_env", default={}
)


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


def input_columns(measure: BaseMeasure, by_name: dict[str, BaseMeasure] | None = None) -> tuple[str, ...]:
    """Physical columns DuckDB must retrieve for this fact. Never a formula."""
    from kpi_engine.core.row_pipeline import physical_input_columns

    if by_name:
        return physical_input_columns(measure, by_name)
    if measure.lookup is not None:
        cols = (measure.lookup.column,)
        if measure.columns:
            return tuple(dict.fromkeys([*measure.columns, *cols]))
        return cols
    if measure.over is not None:
        names = list(measure.over.partition_by) + list(measure.over.order_by)
        if measure.over.of:
            names.append(measure.over.of)
        if measure.columns:
            names = list(measure.columns) + names
        return tuple(dict.fromkeys(names))
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
    if measure.lookup is not None or measure.over is not None:
        return True
    if measure.sql and not is_simple_ident(measure.sql):
        return True
    return False


def _fn_key(registry: dict[str, Any], name: str) -> str | None:
    """Registered spelling of `name`, or None."""
    if name in registry:
        return name
    lowered = name.lower()
    for key in registry:
        if key.lower() == lowered:
            return key
    return None


def _has_string(node: Expr) -> bool:
    """True when a subtree contains a string literal."""
    if isinstance(node, String):
        return True
    if isinstance(node, (Ident, Number, Null)):
        return False
    if isinstance(node, ListLit):
        return any(_has_string(item) for item in node.items)
    if isinstance(node, In):
        return _has_string(node.left) or _has_string(node.right)
    if isinstance(node, (Unary, Not, IsNull)):
        return _has_string(node.operand)
    if isinstance(node, Group):
        return _has_string(node.inner)
    if isinstance(node, Call):
        return any(_has_string(arg) for arg in node.args)
    if isinstance(node, Case):
        return any(_has_string(part) for cond, value in node.whens for part in (cond, value)) or (
            node.else_ is not None and _has_string(node.else_)
        )
    return _has_string(node.left) or _has_string(node.right)


def _ident_series(node: Ident, frame: pd.DataFrame, *, raw: bool) -> pd.Series:
    """One extract column, numeric unless a string comparison needs the raw values."""
    actual = _frame_column(frame, node.name)
    if actual is None:
        raise CatalogError(f"Expression names column {node.name!r}, which is not on the extract.")
    column = frame[actual]
    if raw:
        return column
    if pd.api.types.is_datetime64_any_dtype(column):
        return column
    return pd.to_numeric(column, errors="coerce")


def eval_expr_series(node: Expr, frame: pd.DataFrame, *, raw: bool = False) -> pd.Series:
    """Evaluate an expression on one row of the extract at a time."""
    if isinstance(node, Ident):
        env = _EXPR_ENV.get()
        actual = match_name(node.name, env) if env else None
        if actual is not None:
            value = env[actual]
            if isinstance(value, (list, dict)):
                raise CatalogError(
                    f"Parameter {node.name!r} is not a scalar in this expr."
                )
            return pd.Series(value, index=frame.index)
        return _ident_series(node, frame, raw=raw)
    if isinstance(node, Number):
        return pd.Series(node.value, index=frame.index, dtype="float64")
    if isinstance(node, String):
        return pd.Series(node.value, index=frame.index)
    if isinstance(node, Null):
        return pd.Series(pd.NA, index=frame.index, dtype="object")
    if isinstance(node, Unary):
        series = eval_expr_series(node.operand, frame)
        return series if node.op == "+" else -series
    if isinstance(node, Group):
        return eval_expr_series(node.inner, frame, raw=raw)
    if isinstance(node, Not):
        return _not_series(eval_expr_series(node.operand, frame))
    if isinstance(node, IsNull):
        series = eval_expr_series(node.operand, frame, raw=True)
        flag = series.isna() if not node.invert else series.notna()
        return flag.astype("float64")
    if isinstance(node, Compare):
        use_raw = _has_string(node.left) or _has_string(node.right)
        left = eval_expr_series(node.left, frame, raw=use_raw)
        right = eval_expr_series(node.right, frame, raw=use_raw)
        return _compare_series(node.op, left, right)
    if isinstance(node, In):
        left = eval_expr_series(node.left, frame, raw=True)
        members = _in_members(node.right, dict(_EXPR_ENV.get() or {}))
        if not members:
            return pd.Series(0.0, index=frame.index)
        flag = left.isin(members)
        return flag.astype("float64").mask(left.isna())
    if isinstance(node, BoolOp):
        left = eval_expr_series(node.left, frame)
        right = eval_expr_series(node.right, frame)
        return _bool_series(node.op, left, right)
    if isinstance(node, Call):
        return _call_series(node, frame)
    if isinstance(node, Case):
        return _case_series(node, frame, raw=raw)
    if isinstance(node, Binary):
        left = eval_expr_series(node.left, frame)
        right = eval_expr_series(node.right, frame)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        return _series_div(left, right)
    raise CatalogError(f"Unsupported expression node {type(node).__name__}.")


def eval_expr_scalar(node: Expr, values: dict[str, Any]) -> float | None:
    """Evaluate an expression over other measures' scalars."""
    if isinstance(node, Ident):
        actual = match_name(node.name, values) or (node.name if node.name in values else None)
        if actual is None:
            raise CatalogError(f"Expression names measure {node.name!r}, which was not computed.")
        value = values[actual]
        if isinstance(value, (list, dict)):
            raise CatalogError(
                f"Parameter {node.name!r} is not a scalar in this expr."
            )
        return None if value is None or (isinstance(value, float) and pd.isna(value)) else float(value)
    if isinstance(node, Number):
        return float(node.value)
    if isinstance(node, String):
        raise CatalogError("Measure expr cannot evaluate a string as a number.")
    if isinstance(node, Null):
        return None
    if isinstance(node, Unary):
        value = eval_expr_scalar(node.operand, values)
        if value is None:
            return None
        return value if node.op == "+" else -value
    if isinstance(node, Group):
        return eval_expr_scalar(node.inner, values)
    if isinstance(node, Not):
        value = eval_expr_scalar(node.operand, values)
        if value is None:
            return None
        return 0.0 if value != 0 else 1.0
    if isinstance(node, IsNull):
        value = eval_expr_scalar(node.operand, values)
        missing = value is None
        return 1.0 if missing != node.invert else 0.0
    if isinstance(node, Compare):
        if _has_string(node.left) or _has_string(node.right):
            left_s = _scalar_raw(node.left, values)
            right_s = _scalar_raw(node.right, values)
            if left_s is None or right_s is None:
                return None
            return _compare_scalar_raw(node.op, left_s, right_s)
        left = eval_expr_scalar(node.left, values)
        right = eval_expr_scalar(node.right, values)
        if left is None or right is None:
            return None
        return _compare_scalar(node.op, left, right)
    if isinstance(node, In):
        left = _scalar_raw(node.left, values)
        members = _in_members(node.right, values)
        if left is None:
            return None
        return 1.0 if left in members else 0.0
    if isinstance(node, BoolOp):
        left = eval_expr_scalar(node.left, values)
        right = eval_expr_scalar(node.right, values)
        return _bool_scalar(node.op, left, right)
    if isinstance(node, Call):
        return _call_scalar(node, values)
    if isinstance(node, Case):
        return _case_scalar(node, values)
    if isinstance(node, Binary):
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
    raise CatalogError(f"Unsupported expression node {type(node).__name__}.")


def _not_series(series: pd.Series) -> pd.Series:
    """1 where false, 0 where true, null where null."""
    numeric = pd.to_numeric(series, errors="coerce")
    out = pd.Series(pd.NA, index=series.index, dtype="Float64")
    out = out.mask(numeric.notna() & (numeric == 0), 1.0)
    out = out.mask(numeric.notna() & (numeric != 0), 0.0)
    return out.astype("float64")


def _compare_series(op: str, left: pd.Series, right: pd.Series) -> pd.Series:
    """Row-wise comparison as 1/0/null."""
    if op == "=":
        mask = left == right
    elif op in {"<>", "!="}:
        mask = left != right
    elif op == "<":
        mask = left < right
    elif op == ">":
        mask = left > right
    elif op == "<=":
        mask = left <= right
    else:
        mask = left >= right
    out = mask.astype("float64")
    return out.mask(left.isna() | right.isna())


def _bool_series(op: str, left: pd.Series, right: pd.Series) -> pd.Series:
    """SQL-ish AND/OR on 1/0/null series."""
    lnum = pd.to_numeric(left, errors="coerce")
    rnum = pd.to_numeric(right, errors="coerce")
    ltrue = lnum.notna() & (lnum != 0)
    rtrue = rnum.notna() & (rnum != 0)
    lfalse = lnum.notna() & (lnum == 0)
    rfalse = rnum.notna() & (rnum == 0)
    out = pd.Series(pd.NA, index=left.index, dtype="Float64")
    if op == "and":
        out = out.mask(ltrue & rtrue, 1.0)
        out = out.mask(lfalse | rfalse, 0.0)
    else:
        out = out.mask(ltrue | rtrue, 1.0)
        out = out.mask(lfalse & rfalse, 0.0)
    return out.astype("float64")


def _call_series(node: Call, frame: pd.DataFrame) -> pd.Series:
    """Dispatch an expr call to a column function."""
    key = _fn_key(COLUMN_FNS, node.name)
    if key is None:
        raise CatalogError(
            f"Unknown column function {node.name!r}. Registered: {sorted(COLUMN_FNS)}."
        )
    args = [eval_expr_series(arg, frame) for arg in node.args]
    problem = column_op_error(key, len(args))
    if problem:
        raise CatalogError(f"Column op {problem}")
    result = COLUMN_FNS[key](*args)
    if not isinstance(result, pd.Series):
        raise CatalogError(
            f"Column op {key!r} must return a pandas Series (got {type(result).__name__})."
        )
    return result.reindex(frame.index) if not result.index.equals(frame.index) else result


def _case_series(node: Case, frame: pd.DataFrame, *, raw: bool = False) -> pd.Series:
    """First matching WHEN; ELSE or null. `raw` keeps THEN/ELSE text for count."""
    result = (
        eval_expr_series(node.else_, frame, raw=raw)
        if node.else_ is not None
        else pd.Series(pd.NA, index=frame.index, dtype="object")
    )
    for cond, value in reversed(node.whens):
        numeric = pd.to_numeric(eval_expr_series(cond, frame), errors="coerce")
        pick = numeric.notna() & (numeric != 0)
        result = eval_expr_series(value, frame, raw=raw).where(pick, result)
    return result


def _scalar_missing(value: Any) -> bool:
    """True when a measure scalar is null."""
    return value is None or (isinstance(value, float) and pd.isna(value))


def _scalar_raw(node: Expr, values: dict[str, Any]) -> Any:
    """Evaluate a compare side without forcing float (for 'O')."""
    if isinstance(node, String):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, Ident):
        actual = match_name(node.name, values) or (node.name if node.name in values else None)
        if actual is None:
            raise CatalogError(f"Expression names measure {node.name!r}, which was not computed.")
        return values[actual]
    if isinstance(node, Number):
        return node.value
    if isinstance(node, Group):
        return _scalar_raw(node.inner, values)
    if isinstance(node, ListLit):
        return [_list_item_value(item, values) for item in node.items]
    return eval_expr_scalar(node, values)


def _list_item_value(node: Expr, values: dict[str, Any]) -> Any:
    inner = node
    while isinstance(inner, Group):
        inner = inner.inner
    if isinstance(inner, String):
        return inner.value
    if isinstance(inner, Number):
        return inner.value if inner.text.find(".") != -1 else int(inner.value) if inner.value == int(inner.value) else inner.value
    if isinstance(inner, Null):
        return None
    return _scalar_raw(inner, values)


def _in_members(node: Expr, values: dict[str, Any]) -> list[Any]:
    inner = node
    while isinstance(inner, Group):
        inner = inner.inner
    if isinstance(inner, ListLit):
        return [_list_item_value(item, values) for item in inner.items]
    if isinstance(inner, Ident):
        actual = match_name(inner.name, values) or (inner.name if inner.name in values else None)
        if actual is None:
            raise CatalogError(
                f"Expression names list parameter {inner.name!r}, which was not bound."
            )
        value = values[actual]
        if not isinstance(value, list):
            raise CatalogError(
                f"`in` right side {inner.name!r} must be a list (got {type(value).__name__})."
            )
        return list(value)
    raise CatalogError("`in` right side must be a list parameter or a list literal.")


def _compare_scalar(op: str, left: float, right: float) -> float:
    """Numeric comparison as 1/0."""
    if op == "=":
        return 1.0 if left == right else 0.0
    if op in {"<>", "!="}:
        return 1.0 if left != right else 0.0
    if op == "<":
        return 1.0 if left < right else 0.0
    if op == ">":
        return 1.0 if left > right else 0.0
    if op == "<=":
        return 1.0 if left <= right else 0.0
    return 1.0 if left >= right else 0.0


def _compare_scalar_raw(op: str, left: Any, right: Any) -> float | None:
    """Comparison that may involve strings."""
    if _scalar_missing(left) or _scalar_missing(right):
        return None
    if op == "=":
        return 1.0 if left == right else 0.0
    if op in {"<>", "!="}:
        return 1.0 if left != right else 0.0
    try:
        return _compare_scalar(op, float(left), float(right))
    except (TypeError, ValueError) as exc:
        raise CatalogError("Measure expr comparison needs numbers or equal strings.") from exc


def _bool_scalar(op: str, left: float | None, right: float | None) -> float | None:
    """SQL-ish AND/OR on scalars."""
    ltrue = left is not None and left != 0
    rtrue = right is not None and right != 0
    lfalse = left is not None and left == 0
    rfalse = right is not None and right == 0
    if op == "and":
        if ltrue and rtrue:
            return 1.0
        if lfalse or rfalse:
            return 0.0
        return None
    if ltrue or rtrue:
        return 1.0
    if lfalse and rfalse:
        return 0.0
    return None


_DATE_MEASURE_FNS = frozenset({"date_diff", "date_add", "epoch_day"})


def _call_scalar(node: Call, values: dict[str, Any]) -> float | None:
    """Dispatch an expr call to a measure function."""
    key = _fn_key(MEASURE_FNS, node.name)
    if key is None:
        raise CatalogError(
            f"Unknown measure function {node.name!r}. Registered: {sorted(MEASURE_FNS)}."
        )
    if key in _DATE_MEASURE_FNS:
        args = [_scalar_raw(arg, values) for arg in node.args]
        return call_measure_fn(key, args)
    args = [eval_expr_scalar(arg, values) for arg in node.args]
    return call_measure_fn(key, args)


def _case_scalar(node: Case, values: dict[str, Any]) -> float | None:
    """First matching WHEN; ELSE or null."""
    for cond, value in node.whens:
        flag = eval_expr_scalar(cond, values)
        if flag is not None and flag != 0:
            return eval_expr_scalar(value, values)
    if node.else_ is None:
        return None
    return eval_expr_scalar(node.else_, values)


def _series_div(left: pd.Series, right: pd.Series) -> pd.Series:
    """Divide row by row, treating a zero denominator as null rather than inf."""
    return left / right.replace(0, pd.NA)


def fold_extract_columns(
    frame: pd.DataFrame, kpi: KpiSpec, grain: tuple[str, ...] | None = None
) -> pd.DataFrame:
    """Rename retrieved columns to the KPI YAML spellings they fold onto.

    DuckDB / host context may return Amount or Event_Month. Pandas facts,
    groupby, and densify require the YAML names (amount, event_month).
    When ``grain`` is passed, only projected grouping columns are folded.
    """
    if frame is None or frame.empty:
        return frame
    wanted: list[str] = []
    if kpi.time is not None:
        wanted.append(kpi.time.column)
    if grain is None:
        wanted.extend(kpi.dimensions)
        for spec in kpi.dimension_specs:
            wanted.append(spec.name)
            if spec.source:
                wanted.append(spec.source)
    else:
        wanted.extend(grain)
        grain_fold = {norm_name(name) for name in grain}
        for spec in kpi.dimension_specs:
            physical = spec.source or spec.name
            if (
                norm_name(spec.name) not in grain_fold
                and norm_name(physical) not in grain_fold
            ):
                continue
            wanted.append(spec.name)
            if spec.source:
                wanted.append(spec.source)
    for measure in kpi.base_measures:
        wanted.extend(input_columns(measure, {m.name: m for m in kpi.base_measures}))
        if measure.where is not None:
            wanted.append(measure.where.column)
        if measure.lookup is not None:
            wanted.append(measure.lookup.column)
        if measure.over is not None:
            wanted.extend(measure.over.partition_by)
            wanted.extend(measure.over.order_by)
            if measure.over.of:
                wanted.append(measure.over.of)
    rename: dict[str, str] = {}
    taken: set[str] = set()
    for yaml_name in wanted:
        actual = match_name(yaml_name, frame.columns)
        if actual is None or actual == yaml_name:
            continue
        if yaml_name in frame.columns or yaml_name in taken:
            continue
        rename[actual] = yaml_name
        taken.add(yaml_name)
    if not rename:
        return frame
    return frame.rename(columns=rename)


def _frame_column(frame: pd.DataFrame, name: str) -> str | None:
    """YAML column name or the host spelling that folds onto it."""
    if name in frame.columns:
        return name
    return match_name(name, frame.columns)


@traced
def apply_dimension_maps(frame: pd.DataFrame, kpi: KpiSpec) -> pd.DataFrame:
    """Rewrite dimension columns from `from` + `map` / `grain` after retrieve."""
    if frame.empty or not kpi.dimension_specs:
        return frame
    work = fold_extract_columns(frame, kpi)
    if work is frame:
        work = frame.copy()
    for spec in kpi.dimension_specs:
        src = spec.source or spec.name
        actual = _frame_column(work, src)
        if actual is None:
            continue
        series = work[actual]
        if spec.grain:
            series = pd.to_datetime(series, errors="coerce").dt.to_period(
                {"day": "D", "week": "W-MON", "month": "M", "quarter": "Q", "year": "Y"}[
                    spec.grain
                ]
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
    """Compute every base measure from retrieved physical columns (topo order)."""
    from kpi_engine.core.row_pipeline import apply_lookup, apply_over, topo_bases

    if frame.empty:
        return frame
    token = _EXPR_ENV.set(dict(kpi.bound_parameters))
    try:
        work = fold_extract_columns(frame, kpi)
        if work is frame:
            work = frame.copy()
        retrieved = {str(c) for c in work.columns}
        by_name = {m.name: m for m in kpi.base_measures}
        for measure in topo_bases(kpi.base_measures):
            writes = bool(measure.expr or measure.lookup or measure.over)
            if writes and measure.name in retrieved and not measure.replace:
                raise CatalogError(
                    f"base_measures.{measure.name} would overwrite extract column "
                    f"{measure.name!r}. Set replace: true if that is intended."
                )
            if measure.lookup is not None:
                series = apply_lookup(work, measure.lookup, name=measure.name)
            elif measure.over is not None:
                series = apply_over(work, measure)
            else:
                series = _base_measure_series(work, measure, by_name)
            work[measure.name] = series
        return work
    finally:
        _EXPR_ENV.reset(token)


def _base_measure_series(
    work: pd.DataFrame, measure: BaseMeasure, by_name: dict[str, BaseMeasure] | None = None
) -> pd.Series:
    """One row-level series for a base measure (op, expr, sql column, or where)."""
    cols = input_columns(measure)
    resolved = [_frame_column(work, c) for c in cols]
    missing = [c for c, actual in zip(cols, resolved) if actual is None]
    if missing:
        # Later row steps may already be on `work` under the helper name.
        missing = [c for c in missing if c not in work.columns]
    if missing:
        raise CatalogError(
            f"base_measures.{measure.name} needs columns {missing} on the extract."
        )
    cols = tuple(actual or col for col, actual in zip(cols, resolved))
    keep_raw = measure.agg in COUNT_AGGS
    if measure.row_op is not None and measure.row_op not in PASSTHROUGH_OPS:
        series = apply_row_op(work, cols, measure.row_op, measure.column_params)
    elif measure.expr:
        series = eval_expr_series(
            parse_expression(measure.expr, what="measure expr"), work, raw=keep_raw
        )
    elif measure.sql and not is_simple_ident(measure.sql):
        series = eval_expr_series(
            parse_expression(measure.sql, what="measure sql"), work, raw=keep_raw
        )
    elif cols:
        if keep_raw:
            series = work[cols[0]]
        else:
            series = apply_row_op(work, cols[:1], "value")
    elif measure.name in work.columns:
        series = work[measure.name] if keep_raw else pd.to_numeric(
            work[measure.name], errors="coerce"
        )
    else:
        raise CatalogError(f"base_measures.{measure.name} has no columns to compute.")
    if measure.where is not None:
        series = series.where(apply_where_mask(work, measure.where))
    return series


def apply_where_mask(frame: pd.DataFrame, spec: MeasureWhere) -> pd.Series:
    """Boolean mask for where.column op values. Bind list is the allowed set."""
    actual = _frame_column(frame, spec.column)
    if actual is None:
        raise CatalogError(f"where.column {spec.column!r} is not on the extract.")
    op = spec.op.lower()
    if op not in WHERE_OPS:
        raise CatalogError(f"Unknown where.op {spec.op!r}. Use {WHERE_OPS_HELP}.")
    col = frame[actual]
    if op in NUMERIC_WHERE_OPS:
        col = pd.to_numeric(col, errors="coerce")
    return pandas_mask(col, op, spec.values)


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
            "Add it under capabilities/functions/ and registries/functions/column.yaml."
        )
    if not columns:
        raise CatalogError("Column op needs `columns:` (or sql:).")
    problem = column_op_error(name, len(columns), params)
    if problem:
        raise CatalogError(f"Column op {problem}")
    args = [_column_series(frame, c) for c in columns]
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


def _column_series(frame: pd.DataFrame, name: str) -> pd.Series:
    """Numeric series, or datetime as stored (for date fns / max of a date)."""
    series = frame[name]
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    if series.dtype == object:
        from datetime import date, datetime

        sample = series.dropna()
        if not sample.empty and isinstance(
            sample.iloc[0], (date, datetime, pd.Timestamp)
        ):
            return series
    return pd.to_numeric(series, errors="coerce")


def _fold(step: Callable[[Any, Any], Any], args: tuple[Any, ...]) -> Any:
    """Apply a two-argument step left to right across every operand."""
    result = args[0]
    for item in args[1:]:
        result = step(result, item)
    return result


def call_measure_fn(
    fn: str,
    values: list[Any],
    params: tuple[str, ...] = (),
    extras: Mapping[str, Any] | None = None,
) -> Any:
    """Call a registered measure function with one argument per input measure.

    A strictly two-argument function given more than two values is folded left to
    right, which is what `arithmetic` with a three-name `of:` list has always meant.
    `extras` are YAML `params:` literals that match optional function keywords.
    """
    step = MEASURE_FNS.get(fn)
    if step is None:
        raise CatalogError(
            f"Unknown measure fn {fn!r}. Registered: {sorted(MEASURE_FNS)}. "
            "Add it under capabilities/functions/ and registries/functions/measure.yaml."
        )
    if not values:
        return None
    problem = measure_fn_error(fn, len(values), params)
    if problem:
        raise CatalogError(f"Measure fn {problem}")
    names = set(measure_fn_meta(fn).params)
    kwargs = {
        key: extras[key]
        for key in (extras or {})
        if key in names and key not in params
    }
    if params:
        return step(**dict(zip(params, values)), **kwargs)
    if measure_fn_meta(fn).max_args == 2 and len(values) > 2:
        return _fold(step, tuple(values))
    return step(*values, **kwargs)


def _sum_or_null(series: pd.Series) -> float:
    """Sum that stays null when every value is null (pandas default would be 0)."""
    return series.sum(min_count=1)


def pandas_group_keys(kpi: KpiSpec, grain: tuple[str, ...]) -> list[str]:
    """Grouping columns after dimension maps (mapped name, not `from`)."""
    time_col = kpi.time.column if kpi.time is not None else None
    rename = {spec.source: spec.name for spec in kpi.dimension_specs if spec.source}
    keys: list[str] = []
    for col in grain:
        if time_col is not None and norm_name(col) == norm_name(time_col):
            continue
        keys.append(rename.get(col, col))
    return keys


@traced
def collapse_pandas_detail(
    detail: pd.DataFrame,
    kpi: KpiSpec,
    grain: tuple[str, ...],
    *,
    facts_applied: bool = False,
    measure_keys: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Compute KPI YAML facts per row, then fold additive aggs to the extract grain."""
    from kpi_engine.core.model_sql import NON_ADDITIVE
    from kpi_engine.core.row_pipeline import is_helper

    if detail is None or detail.empty:
        return pd.DataFrame()
    if facts_applied:
        work = detail.copy()
    else:
        work = fold_extract_columns(detail, kpi, grain)
        work = apply_dimension_maps(work, kpi)
        work = apply_pandas_facts(work, kpi)
    time_col = kpi.time.column if kpi.time is not None else None
    if time_col is not None:
        actual_time = _frame_column(work, time_col)
        if actual_time is not None and actual_time != time_col:
            work = work.rename(columns={actual_time: time_col})
    keys = list(pandas_group_keys(kpi, grain))
    if time_col is not None and time_col in work.columns:
        keys = [time_col, *keys]
    keys = [c for c in keys if c in work.columns]
    aggs: dict[str, Any] = {}
    foldable = {"sum", "avg", "min", "max", "count", "first", "last"}
    from kpi_engine.capabilities.ops.support import helper_names_used_as_of

    keys_for_helpers = (
        measure_keys if measure_keys is not None else tuple(m.key for m in kpi.measures)
    )
    needed_helpers = helper_names_used_as_of(kpi, keys_for_helpers)
    if needed_helpers:
        if not keys:
            if len(work) > 1:
                raise CatalogError(
                    f"identity_grain is not unique ({len(work)} rows in the extract)."
                )
        else:
            present = [c for c in keys if c in work.columns]
            if present:
                sizes = work.groupby(present, dropna=False).size()
                if not sizes.empty and int(sizes.max()) > 1:
                    raise CatalogError(
                        "identity_grain is not unique: some groups have more than "
                        f"one row (max {int(sizes.max())})."
                    )
    for measure in kpi.base_measures:
        if measure.name in needed_helpers and measure.name in work.columns:
            aggs[measure.name] = "first"
            continue
        if is_helper(measure) or measure.name not in work.columns:
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
    drop_internal = [c for c in ("_kpi_row_id",) if c in work.columns]
    if drop_internal:
        work = work.drop(columns=drop_internal)
    if not aggs:
        return pd.DataFrame()
    if time_col is not None and time_col in work.columns:
        work = work.sort_values(time_col)
    if not keys:
        out = work[list(aggs)].agg(aggs)
        return out.to_frame().T if isinstance(out, pd.Series) else out.reset_index(drop=True)
    grouped = work.groupby(keys, dropna=False, as_index=False).agg(aggs)
    return grouped


def _autoload() -> None:
    """Fill COLUMN_FNS / MEASURE_FNS from registries/ on first import."""
    from kpi_engine.core.loader import ensure_loaded

    ensure_loaded()


_autoload()
