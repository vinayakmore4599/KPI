"""Named row steps, lookups, and entity windows on the pre-fold detail frame."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from kpi_engine.contracts import (
    OVER_FNS,
    WINDOW_AGGS_NEED_IDENTITY,
    BaseMeasure,
    KpiSpec,
    LookupSpec,
    OverSpec,
    extra_retrieve_columns,
)
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.identifiers import (
    expression_columns,
    is_simple_ident,
    match_name,
    parse_expression,
)

OVER_ROW_CAP = 500_000
OVER_PARTITION_CAP = 50_000


def is_helper(measure: BaseMeasure) -> bool:
    """True when the base is a row-only step (no fold agg)."""
    return measure.agg is None


def topo_bases(bases: tuple[BaseMeasure, ...]) -> tuple[BaseMeasure, ...]:
    """Order bases so expr/lookup/over see earlier names. Cycles are BindError."""
    by_name = {m.name: m for m in bases}
    names = [m.name for m in bases]
    deps = {m.name: _base_deps(m, by_name) for m in bases}
    state: dict[str, int] = {}
    ordered: list[str] = []

    def walk(name: str, trail: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            cycle = trail[trail.index(name) :] + [name]
            raise BindError(
                f"base_measures dependency cycle: {' -> '.join(cycle)}. "
                "A row step cannot name itself, directly or indirectly."
            )
        state[name] = 1
        trail.append(name)
        for dep in deps.get(name, ()):
            if dep in by_name:
                walk(dep, trail)
        trail.pop()
        state[name] = 2
        ordered.append(name)

    for name in names:
        walk(name, [])
    return tuple(by_name[name] for name in ordered)


def base_dep_names(measure: BaseMeasure, known: Iterable[str]) -> tuple[str, ...]:
    """Other base_measures this step reads (not physical columns)."""
    known_set = {name for name in known}
    return tuple(
        n for n in _raw_dep_names(measure) if n in known_set and n != measure.name
    )


def physical_input_columns(
    measure: BaseMeasure, by_name: dict[str, BaseMeasure] | None = None
) -> tuple[str, ...]:
    """Columns DuckDB must retrieve. Walks helper names to physical columns."""
    by_name = by_name or {}
    seen: set[str] = set()
    out: list[str] = []

    def walk(item: BaseMeasure, trail: set[str]) -> None:
        if item.name in trail:
            return
        nested = trail | {item.name}
        for raw in _raw_dep_names(item):
            if raw in by_name and raw != item.name:
                walk(by_name[raw], nested)
                continue
            if raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
        for col in extra_retrieve_columns(item):
            if col in seen or col in by_name:
                continue
            seen.add(col)
            out.append(col)

    walk(measure, set())
    return tuple(out)


def stabilize_detail(frame: pd.DataFrame, kpi: KpiSpec) -> pd.DataFrame:
    """Deterministic row order plus `_kpi_row_id` after retrieve."""
    if frame is None or frame.empty:
        return frame
    extra: list[str] = []
    for measure in kpi.base_measures:
        if measure.over is None:
            continue
        extra.extend(measure.over.order_by)
        extra.extend(measure.over.partition_by)
    cols: list[str] = []
    if kpi.time is not None and kpi.time.column in frame.columns:
        cols.append(kpi.time.column)
    for dim in kpi.dimensions:
        if dim in frame.columns and dim not in cols:
            cols.append(dim)
    for name in extra:
        actual = name if name in frame.columns else match_name(name, frame.columns)
        if actual is not None and actual not in cols:
            cols.append(actual)
    work = frame.sort_values(cols, kind="mergesort", na_position="last") if cols else frame.copy()
    work = work.reset_index(drop=True)
    work["_kpi_row_id"] = range(len(work))
    return work


def apply_lookup(
    frame: pd.DataFrame,
    spec: LookupSpec,
    *,
    name: str,
    kpi: KpiSpec | None = None,
    as_of_default: Any = None,
) -> pd.Series:
    """Map one or more columns through a static dict. Unknown → default else null."""
    key_cols = list(spec.keys) if spec.keys else [spec.column]
    actuals = [_require_col(frame, col, name, "lookup") for col in key_cols]
    if len(actuals) == 1:
        keys = frame[actuals[0]].map(_lookup_key)
    else:
        keys = pd.Series(
            [
                _composite_lookup_key(tuple(frame.loc[idx, c] for c in actuals))
                for idx in frame.index
            ],
            index=frame.index,
        )
    mapped = keys.map(spec.mapping)
    unknown = keys.notna() & mapped.isna()
    if spec.strict and bool(unknown.any()):
        sample = keys[unknown].iloc[0]
        raise CatalogError(
            f"base_measures.{name} lookup strict=true saw unknown key {sample!r}."
        )
    if spec.default is not None:
        mapped = mapped.where(~unknown, spec.default)
    if spec.valid_from or spec.valid_to:
        mapped = _apply_lookup_as_of(
            frame, spec, mapped, name=name, kpi=kpi, as_of_default=as_of_default
        )
    return mapped


def _composite_lookup_key(values: tuple[Any, ...]) -> str | None:
    pieces: list[str] = []
    for value in values:
        key = _lookup_key(value)
        if key is None:
            return None
        pieces.append(key)
    return "|".join(pieces)


def _apply_lookup_as_of(
    frame: pd.DataFrame,
    spec: LookupSpec,
    mapped: pd.Series,
    *,
    name: str,
    kpi: KpiSpec | None,
    as_of_default: Any,
) -> pd.Series:
    """Keep mapped values only when as-of sits in [valid_from, valid_to]."""
    if spec.as_of and spec.as_of != "anchor":
        col = _require_col(frame, spec.as_of, name, "lookup.as_of")
        as_of = pd.to_datetime(frame[col], errors="coerce")
    elif as_of_default is not None:
        as_of = pd.to_datetime(pd.Series(as_of_default, index=frame.index), errors="coerce")
    elif kpi is not None and kpi.time is not None and kpi.time.column in frame.columns:
        as_of = pd.to_datetime(frame[kpi.time.column], errors="coerce")
    else:
        as_of = pd.Series(pd.NaT, index=frame.index)
    in_range = pd.Series(True, index=frame.index)
    if spec.valid_from:
        col = _require_col(frame, spec.valid_from, name, "lookup.valid_from")
        valid_from = pd.to_datetime(frame[col], errors="coerce")
        in_range &= as_of.isna() | valid_from.isna() | (as_of >= valid_from)
    if spec.valid_to:
        col = _require_col(frame, spec.valid_to, name, "lookup.valid_to")
        valid_to = pd.to_datetime(frame[col], errors="coerce")
        in_range &= as_of.isna() | valid_to.isna() | (as_of <= valid_to)
    return mapped.where(in_range)


def apply_over(frame: pd.DataFrame, measure: BaseMeasure) -> pd.Series:
    """Entity window on pre-fold rows. Caps fail fast; no silent truncate."""
    spec = measure.over
    if spec is None:
        raise CatalogError(f"base_measures.{measure.name} has no over:.")
    n_rows = len(frame)
    if n_rows > OVER_ROW_CAP:
        raise CatalogError(
            f"base_measures.{measure.name} over: on {n_rows} rows exceeds "
            f"{OVER_ROW_CAP}. Narrow filters or pre-aggregate in a SQL model. "
            f"partition_by={list(spec.partition_by)}."
        )
    parts = [_require_col(frame, col, measure.name, "partition_by") for col in spec.partition_by]
    orders = [_require_col(frame, col, measure.name, "order_by") for col in spec.order_by]
    if parts:
        n_parts = int(frame[parts].drop_duplicates().shape[0])
        if n_parts > OVER_PARTITION_CAP:
            raise CatalogError(
                f"base_measures.{measure.name} over: has {n_parts} partitions, cap "
                f"{OVER_PARTITION_CAP}. partition_by={list(spec.partition_by)}."
            )
    order_cols = [*orders, "_kpi_row_id"] if "_kpi_row_id" in frame.columns else list(orders)
    work = frame.sort_values(order_cols, kind="mergesort", na_position="last")
    group = work.groupby(parts, dropna=False, sort=False) if parts else None
    of_col = None
    if spec.of:
        of_col = _require_col(frame, spec.of, measure.name, "over.of")
    n = spec.n if spec.n is not None else 1
    fn = spec.fn
    if fn == "row_number":
        values = _grouped_rank(work, group, method="first")
    elif fn == "rank":
        values = _grouped_rank(work, group, method="min", of_col=of_col)
    elif fn == "dense_rank":
        values = _grouped_rank(work, group, method="dense", of_col=of_col)
    elif fn == "lag":
        values = _shift(work, group, of_col, n)
    elif fn == "lead":
        values = _shift(work, group, of_col, -n)
    elif fn == "running_sum":
        values = _running(work, group, of_col, how="sum")
    elif fn == "running_avg":
        values = _running(work, group, of_col, how="mean")
    elif fn == "last_n":
        values = _last_n(work, parts, of_col, n)
    else:
        raise CatalogError(f"base_measures.{measure.name} unknown over.fn {fn!r}.")
    return values.reindex(frame.index)


def assert_window_agg(measure: BaseMeasure) -> None:
    """BindError when a window fact is summed/averaged unless agg_ok."""
    if measure.over is None or measure.agg is None or measure.agg_ok:
        return
    if measure.over.fn not in OVER_FNS:
        return
    if measure.over.fn == "last_n" and measure.agg not in {"first", "last", "min", "max"}:
        raise BindError(
            f"base_measures.{measure.name} over.fn=last_n cannot use agg={measure.agg!r}. "
            "Use first or last, or set agg_ok: true."
        )
    if measure.agg in WINDOW_AGGS_NEED_IDENTITY:
        raise BindError(
            f"base_measures.{measure.name} over.fn={measure.over.fn} cannot use "
            f"agg={measure.agg!r} (that sums/averages a window). Use first, last, "
            "min, or max, or set agg_ok: true."
        )


def _base_deps(measure: BaseMeasure, by_name: dict[str, BaseMeasure]) -> tuple[str, ...]:
    """Other bases this step reads. A step may name its own extract column (replace:)."""
    return tuple(
        n for n in _raw_dep_names(measure) if n in by_name and n != measure.name
    )


def _raw_dep_names(measure: BaseMeasure) -> tuple[str, ...]:
    names: list[str] = []
    if measure.lookup is not None:
        names.append(measure.lookup.column)
        names.extend(measure.lookup.keys)
        if measure.lookup.valid_from:
            names.append(measure.lookup.valid_from)
        if measure.lookup.valid_to:
            names.append(measure.lookup.valid_to)
        if measure.lookup.as_of and measure.lookup.as_of != "anchor":
            names.append(measure.lookup.as_of)
    if measure.over is not None:
        names.extend(measure.over.partition_by)
        names.extend(measure.over.order_by)
        if measure.over.of:
            names.append(measure.over.of)
    if measure.columns:
        names.extend(measure.columns)
    source = measure.expr or (
        measure.sql if measure.sql and not is_simple_ident(measure.sql) else None
    )
    if source:
        names.extend(expression_columns(parse_expression(source, what="measure sql")))
    elif measure.sql and is_simple_ident(measure.sql):
        names.append(measure.sql.strip())
    return tuple(dict.fromkeys(names))


def _lookup_key(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _require_col(frame: pd.DataFrame, name: str, measure: str, what: str) -> str:
    if name in frame.columns:
        return name
    actual = match_name(name, frame.columns)
    if actual is None:
        raise CatalogError(
            f"base_measures.{measure} {what} {name!r} is not on this extract."
        )
    return actual


def _grouped_rank(
    work: pd.DataFrame,
    group: Any,
    *,
    method: str,
    of_col: str | None = None,
) -> pd.Series:
    if of_col is not None:
        series = work[of_col]
        if group is None:
            return series.rank(method=method, na_option="keep")
        return group[of_col].rank(method=method, na_option="keep")
    if group is None:
        return pd.Series(range(1, len(work) + 1), index=work.index, dtype="float64")
    return group.cumcount() + 1


def _shift(work: pd.DataFrame, group: Any, of_col: str | None, periods: int) -> pd.Series:
    if of_col is None:
        raise CatalogError("over.fn lag/lead requires over.of.")
    if group is None:
        return work[of_col].shift(periods)
    return group[of_col].shift(periods)


def _running(work: pd.DataFrame, group: Any, of_col: str | None, *, how: str) -> pd.Series:
    if of_col is None:
        raise CatalogError("over.fn running_sum/running_avg requires over.of.")
    numeric = pd.to_numeric(work[of_col], errors="coerce")
    if group is None:
        return numeric.cumsum() if how == "sum" else numeric.expanding().mean()
    if how == "sum":
        return group[of_col].transform(lambda s: pd.to_numeric(s, errors="coerce").cumsum())
    return group[of_col].transform(
        lambda s: pd.to_numeric(s, errors="coerce").expanding().mean()
    )


def _last_n(work: pd.DataFrame, parts: list[str], of_col: str | None, n: int) -> pd.Series:
    if of_col is None:
        raise CatalogError("over.fn last_n requires over.of.")
    if n < 1:
        raise CatalogError("over.last_n n must be >= 1.")

    def window(series: pd.Series) -> pd.Series:
        values = list(series)
        out: list[list[Any]] = []
        for i in range(len(values)):
            chunk = values[max(0, i + 1 - n) : i + 1]
            out.append([_jsonish(v) for v in chunk])
        return pd.Series(out, index=series.index)

    if not parts:
        return window(work[of_col])
    grouper: Any = parts[0] if len(parts) == 1 else parts
    out = pd.Series(index=work.index, dtype=object)
    for _, idx in work.groupby(grouper, dropna=False, sort=False).groups.items():
        ranked = window(work.loc[list(idx), of_col])
        out.loc[ranked.index] = ranked
    return out


def _jsonish(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            inner = value.item()
        except (ValueError, AttributeError):
            return value
        if inner is value:
            return value
        return _jsonish(inner)
    return value
