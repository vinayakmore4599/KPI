"""HAVING and op: predicate comparison helpers."""

from __future__ import annotations

from typing import Any

from kpi_engine.contracts import HAVING_CMP, HavingPredicate, OutputSpec
from kpi_engine.exceptions import BindError, CatalogError


def parse_predicates(raw: Any, *, what: str) -> tuple[HavingPredicate, ...]:
    """Parse a list of {of, cmp, value|vs|low/high} objects."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise BindError(f"{what} requires a non-empty predicates: list.")
    out: list[HavingPredicate] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BindError(f"{what} predicates[{i}] must be an object.")
        of = str(item.get("of") or "").strip()
        if not of:
            raise BindError(f"{what} predicates[{i}] needs of: (a measure key).")
        cmp = str(item.get("cmp") or "gte").strip().lower()
        if cmp not in HAVING_CMP:
            raise BindError(
                f"{what} predicates[{i}] cmp must be "
                f"{', '.join(sorted(HAVING_CMP))} (got {cmp!r})."
            )
        vs = item.get("vs")
        vs_name = str(vs) if vs is not None else None
        value = _optional_float(item.get("value"), f"{what} predicates[{i}].value")
        low = _optional_float(item.get("low"), f"{what} predicates[{i}].low")
        high = _optional_float(item.get("high"), f"{what} predicates[{i}].high")
        if cmp == "between":
            if vs_name is not None:
                raise BindError(f"{what} predicates[{i}] between cannot set vs:.")
            if low is None or high is None:
                raise BindError(
                    f"{what} predicates[{i}] cmp=between needs low: and high:."
                )
        elif vs_name is None and value is None:
            raise BindError(
                f"{what} predicates[{i}] requires value: or vs:."
            )
        elif vs_name is not None and value is not None:
            raise BindError(
                f"{what} predicates[{i}] cannot set both value: and vs:."
            )
        out.append(
            HavingPredicate(
                of=of,
                cmp=cmp,
                value=value,
                vs=vs_name,
                low=low,
                high=high,
            )
        )
    return tuple(out)


def parse_match(raw: Any, *, what: str) -> str:
    """all (AND, default) or any (OR)."""
    if raw is None:
        return "all"
    match = str(raw).strip().lower()
    if match not in {"all", "any"}:
        raise BindError(f"{what} match must be all or any (got {raw!r}).")
    return match


def predicate_names(predicates: tuple[HavingPredicate, ...]) -> tuple[str, ...]:
    """Measure keys a predicate list needs on the row."""
    names: list[str] = []
    for pred in predicates:
        names.append(pred.of)
        if pred.vs:
            names.append(pred.vs)
    return tuple(dict.fromkeys(names))


def assert_scalar_ofs(
    predicates: tuple[HavingPredicate, ...],
    measures: tuple[OutputSpec, ...],
    *,
    what: str,
) -> None:
    """Fail when a predicate names a trend array or an unknown key."""
    by_key = {m.key: m for m in measures}
    from kpi_engine.core.op_registry import get_op

    for pred in predicates:
        for name in (pred.of, pred.vs):
            if not name:
                continue
            spec = by_key.get(name)
            if spec is None:
                continue
            if get_op(spec.kind).emits_trend:
                raise BindError(
                    f"{what} of={name!r} is a trend array; having/predicate need a scalar."
                )


def eval_predicate(pred: HavingPredicate, row: dict[str, Any]) -> bool:
    """True when this row passes. Null `of` (or vs) fails the predicate."""
    left = row.get(pred.of)
    if _is_null(left):
        return False
    if pred.cmp == "between":
        try:
            number = float(left)
        except (TypeError, ValueError) as exc:
            raise CatalogError(
                f"having of={pred.of!r} is not numeric (got {left!r})."
            ) from exc
        return float(pred.low) <= number <= float(pred.high)
    right = pred.value if pred.vs is None else row.get(pred.vs)
    if _is_null(right):
        return False
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError) as exc:
        raise CatalogError(
            f"having comparison of={pred.of!r} is not numeric."
        ) from exc
    if pred.cmp == "gt":
        return a > b
    if pred.cmp == "gte":
        return a >= b
    if pred.cmp == "lt":
        return a < b
    if pred.cmp == "lte":
        return a <= b
    if pred.cmp == "eq":
        return a == b
    return a != b


def eval_predicate_list(
    predicates: tuple[HavingPredicate, ...],
    match: str,
    row: dict[str, Any],
) -> bool:
    """AND (all) or OR (any) of predicates on one JSON row."""
    flags = [eval_predicate(pred, row) for pred in predicates]
    if match == "any":
        return any(flags)
    return all(flags)


def _optional_float(raw: Any, what: str) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise BindError(f"{what} must be a number.") from exc


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        return False
