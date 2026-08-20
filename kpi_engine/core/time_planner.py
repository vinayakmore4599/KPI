"""Anchor month and required_span. The month filter never becomes a generic IN.

What this file provides
    plan_time, claim_month_filter, max_lookback_months, lookback_for.

Where it is used
    orchestrator after YAML bind. validate() uses it to report span_start
    without scanning. Tests in test_span.py and test_month_filter.py.

Capabilities
    - Finds time.filter_code on the context (e.g. reporting_month).
    - Missing or multi-value month filter → TimePlanError (no silent default).
    - Lookback from requested measures only (3m vs previous year vs trend).
    - Returns remaining filters for IN binding (month removed).

When to use
    Change lookback formulas when adding a new measure op. Do not apply the
    selected month as WHERE col IN ('2026-03') — that is a correctness bug.
"""

from __future__ import annotations

from kpi_engine.contracts import (
    AdaptedRequest,
    IncomingFilter,
    KpiSpec,
    OutputSpec,
    TimePlan,
)
from kpi_engine.dates import add_months, parse_month
from kpi_engine.exceptions import TimePlanError


def plan_time(request: AdaptedRequest, kpi: KpiSpec) -> tuple[TimePlan, tuple[IncomingFilter, ...]]:
    """Claim the month filter as anchor, compute required_span, return remaining filters."""
    claimed, rest = claim_month_filter(request.filters, kpi.time.filter_code)
    if claimed is None:
        raise TimePlanError(
            f"Missing month filter {kpi.time.filter_code!r}. "
            "The selected month is required and is not defaulted."
        )
    if len(claimed.values) != 1:
        raise TimePlanError(
            f"Month filter {claimed.code!r} must contain exactly one value "
            f"(got {len(claimed.values)})."
        )
    anchor = parse_month(claimed.values[0])
    lookback = max_lookback_months(kpi, request.measure_keys)
    span_start = add_months(anchor, -lookback)
    span_end_exclusive = add_months(anchor, 1)
    return (
        TimePlan(
            anchor=anchor,
            span_start=span_start,
            span_end_exclusive=span_end_exclusive,
            lookback_months=lookback,
            claimed_filter_code=claimed.code,
        ),
        rest,
    )


def claim_month_filter(
    filters: tuple[IncomingFilter, ...], filter_code: str
) -> tuple[IncomingFilter | None, tuple[IncomingFilter, ...]]:
    """Pull the selected-month filter out of the generic IN list so it becomes a range."""
    wanted = _norm(filter_code)
    claimed: IncomingFilter | None = None
    rest: list[IncomingFilter] = []
    for item in filters:
        if claimed is None and _matches_time_filter(item, wanted):
            claimed = item
        else:
            rest.append(item)
    return claimed, tuple(rest)


def max_lookback_months(kpi: KpiSpec, requested: tuple[str, ...]) -> int:
    """Deepest lookback among requested measures only (unrequested keys do not widen the scan)."""
    by_key = {m.key: m for m in kpi.measures}
    keys = requested or tuple(by_key)
    return max((lookback_for(by_key[k], by_key) for k in keys), default=0)


def lookback_for(output: OutputSpec, by_key: dict[str, OutputSpec]) -> int:
    """How many months before the anchor this measure needs (calendar, not row count)."""
    if output.kind == "dimension":
        return 0
    if output.kind == "point":
        return (output.offset.total_months if output.offset else 0)
    if output.kind in {"window", "trend"}:
        n = output.trailing_months or 1
        if output.inclusive:
            return max(n - 1, 0)
        return n
    if output.kind == "arithmetic":
        left = by_key.get(output.left or "")
        right = by_key.get(output.right or "")
        return max(
            lookback_for(left, by_key) if left else 0,
            lookback_for(right, by_key) if right else 0,
        )
    raise TimePlanError(f"Cannot plan lookback for {output.key} kind={output.kind}.")


def _matches_time_filter(item: IncomingFilter, wanted: str) -> bool:
    """True if this context filter is the KPI's time.filter_code."""
    if not wanted:
        return False
    return _norm(item.code) == wanted or _norm(item.raw_key) == wanted


def _norm(value: str) -> str:
    """Case-insensitive compare key (spaces become underscores)."""
    return value.strip().lower().replace(" ", "_")
