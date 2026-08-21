"""Anchor period and required_span. The time filter never becomes a generic IN.

What this file provides
    plan_time, claim_month_filter, max_lookback_months, lookback_for.

Where it is used
    orchestrator after YAML bind. validate() uses it to report span_start
    without scanning.

Capabilities
    - Finds time.filter_code on the context (e.g. reporting_month).
    - Missing or multi-value time filter → TimePlanError (no silent default).
    - Lookback from requested measures only, in KPI grain periods.
    - Gregorian or fiscal truncate via dates.truncate_period.
    - Returns remaining filters for IN binding (time filter removed).
"""

from __future__ import annotations

from kpi_engine.contracts import (
    AdaptedRequest,
    IncomingFilter,
    KpiSpec,
    OutputSpec,
    TimePlan,
    TimeSpec,
)
from kpi_engine.dates import (
    add_periods,
    apply_offset,
    parse_date,
    periods_between,
    truncate_period,
)
from kpi_engine.exceptions import TimePlanError
from kpi_engine.runlog import traced


@traced
def plan_time(request: AdaptedRequest, kpi: KpiSpec) -> tuple[TimePlan, tuple[IncomingFilter, ...]]:
    """Claim the time filter as anchor, compute required_span, return remaining filters."""
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
    raw_anchor = parse_date(claimed.values[0])
    if kpi.time.grain == "day" and len(str(claimed.values[0]).strip()) < 10:
        raise TimePlanError(
            f"time.grain=day requires a full date YYYY-MM-DD on {claimed.code!r}."
        )
    anchor = truncate_period(raw_anchor, kpi.time)
    lookback = max_lookback_months(kpi, request.measure_keys)
    span_start = add_periods(anchor, -lookback, kpi.time)
    span_end_exclusive = add_periods(anchor, 1, kpi.time)
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
    """Pull the selected-period filter out of the generic IN list so it becomes a range."""
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
    return max((lookback_for(by_key[k], by_key, kpi.time) for k in keys), default=0)


def lookback_for(
    output: OutputSpec,
    by_key: dict[str, OutputSpec],
    time: TimeSpec | None = None,
) -> int:
    """How many grain periods before the anchor this measure needs."""
    if output.kind == "dimension":
        return 0
    if output.kind == "point":
        if not output.offset:
            return 0
        if time is None or (
            time.grain == "month"
            and output.offset.days == 0
            and output.offset.quarters == 0
        ):
            return output.offset.total_months
        return _offset_periods(output, time)
    if output.kind == "hook":
        if output.offset:
            if time is None or time.grain == "month":
                return output.offset.total_months
            return _offset_periods(output, time)
        if output.trailing_months:
            n = output.trailing_months
            return max(n - 1, 0) if output.inclusive else n
        return 0
    if output.kind in {"window", "trend"}:
        n = output.trailing_months or 1
        if output.inclusive:
            return max(n - 1, 0)
        return n
    if output.kind == "arithmetic":
        left = by_key.get(output.left or "")
        right = by_key.get(output.right or "")
        return max(
            lookback_for(left, by_key, time) if left else 0,
            lookback_for(right, by_key, time) if right else 0,
        )
    raise TimePlanError(f"Cannot plan lookback for {output.key} kind={output.kind}.")


def _offset_periods(output: OutputSpec, time: TimeSpec) -> int:
    """Count grain steps between dummy-anchor and dummy-anchor minus offset."""
    from datetime import date as date_cls

    dummy = date_cls(2021, 6, 15)
    dummy_t = truncate_period(dummy, time)
    target = truncate_period(apply_offset(dummy, output.offset), time)
    return periods_between(target, dummy_t, time)


def _matches_time_filter(item: IncomingFilter, wanted: str) -> bool:
    """True if this context filter is the KPI's time.filter_code."""
    if not wanted:
        return False
    return _norm(item.code) == wanted or _norm(item.raw_key) == wanted


def _norm(value: str) -> str:
    """Case-insensitive compare key (spaces become underscores)."""
    return value.strip().lower().replace(" ", "_")
