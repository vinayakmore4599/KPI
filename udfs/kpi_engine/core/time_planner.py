"""Anchor period and required_span. The time filter never becomes a generic IN.

What this file provides
    plan_time, claim_month_filter, max_lookback_months, lookback_for.

Where it is used
    orchestrator after YAML bind. validate() uses it to report span_start
    without scanning.

Capabilities
    - Finds time.filter_code on the context (name is per KPI YAML).
    - Snapshot KPIs with no time: block skip the claim.
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
    add_days,
    add_periods,
    apply_offset,
    parse_date,
    periods_between,
    truncate_period,
    year_start,
)
from kpi_engine.core.binder import fold_measure_keys
from kpi_engine.exceptions import TimePlanError
from kpi_engine.runlog import traced


@traced
def plan_time(request: AdaptedRequest, kpi: KpiSpec) -> tuple[TimePlan | None, tuple[IncomingFilter, ...]]:
    """Claim the time filter as anchor, compute required_span, return remaining filters.

    Snapshot KPIs (no YAML time:) skip the claim and leave every filter for IN binding.
    """
    if kpi.time is None:
        return None, request.filters
    claimed, rest = claim_month_filter(request.filters, kpi.time.filter_code)
    if claimed is None:
        raise TimePlanError(
            f"Missing month filter {kpi.time.filter_code!r}. "
            "Set time.filter_code in the KPI YAML to the context filter that "
            "carries the selected period, or omit the time: block if this KPI "
            "has no time column. The selected period is not defaulted."
        )
    if len(claimed.values) != 1:
        raise TimePlanError(
            f"Month filter {claimed.code!r} must contain exactly one value "
            f"(got {len(claimed.values)})."
        )
    raw_anchor = parse_date(claimed.values[0], fmt=kpi.time.format)
    if (
        kpi.time.grain == "day"
        and kpi.time.format is None
        and len(str(claimed.values[0]).strip()) < 10
    ):
        raise TimePlanError(
            f"time.grain=day requires a full date YYYY-MM-DD on {claimed.code!r}."
        )
    anchor = truncate_period(raw_anchor, kpi.time)
    keys = fold_measure_keys(kpi, request.measure_keys)
    lookback = max_lookback_months(kpi, keys, anchor=anchor)
    forward = max_lookforward_periods(kpi, keys)
    span_start = add_periods(anchor, -lookback, kpi.time)
    span_end_exclusive = add_periods(anchor, 1 + forward, kpi.time)
    return (
        TimePlan(
            anchor=anchor,
            span_start=span_start,
            span_end_exclusive=span_end_exclusive,
            lookback_months=lookback,
            claimed_filter_code=claimed.code,
            lookback_forward=forward,
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


def max_lookback_months(
    kpi: KpiSpec, requested: tuple[str, ...], anchor=None
) -> int:
    """Deepest lookback among requested measures only (unrequested keys do not widen the scan)."""
    keys = fold_measure_keys(kpi, requested)
    by_key = {m.key: m for m in kpi.measures}
    return max(
        (lookback_for(by_key[k], by_key, kpi.time, anchor=anchor) for k in keys if k in by_key),
        default=0,
    )


def max_lookforward_periods(kpi: KpiSpec, requested: tuple[str, ...]) -> int:
    """Periods after the anchor needed for leading windows."""
    keys = fold_measure_keys(kpi, requested)
    by_key = {m.key: m for m in kpi.measures}
    return max((lookforward_for(by_key[k], by_key) for k in keys if k in by_key), default=0)


def lookback_for(
    output: OutputSpec,
    by_key: dict[str, OutputSpec],
    time: TimeSpec | None = None,
    anchor=None,
    seen: frozenset[str] = frozenset(),
) -> int:
    """How many grain periods before the anchor this measure needs.

    `seen` guards against a dependency cycle. Cycles are rejected in the binder;
    this keeps span planning terminating even if a KpiSpec is built by hand.
    """
    if output.key in seen:
        return 0
    if output.kind in {"dimension", "constant"}:
        return 0
    if output.kind == "rank":
        if output.of and output.of in by_key:
            return lookback_for(
                by_key[output.of], by_key, time, anchor=anchor, seen=seen | {output.key}
            )
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
        kind = output.window_range or "trailing"
        if kind == "leading":
            return 0
        if kind == "cumulative":
            if time is None:
                return 0
            from datetime import date as date_cls

            ref = truncate_period(anchor or date_cls(2021, 12, 15), time)
            start = year_start(ref, time)
            return periods_between(start, ref, time)
        n = output.trailing_months or 1
        if output.trailing_unit == "day" and time is not None and time.grain != "day":
            from datetime import date as date_cls

            ref = truncate_period(anchor or date_cls(2021, 6, 15), time)
            start = add_days(ref, -(n - 1 if output.inclusive else n))
            return periods_between(truncate_period(start, time), ref, time)
        if output.inclusive:
            return max(n - 1, 0)
        return n
    if output.kind in {"arithmetic", "fn", "expr"}:
        deeper = seen | {output.key}
        return max(
            (
                lookback_for(by_key[n], by_key, time, anchor=anchor, seen=deeper)
                for n in _operand_keys(output)
                if n in by_key
            ),
            default=0,
        )
    raise TimePlanError(f"Cannot plan lookback for {output.key} kind={output.kind}.")


def lookforward_for(
    output: OutputSpec,
    by_key: dict[str, OutputSpec],
    seen: frozenset[str] = frozenset(),
) -> int:
    """How many grain periods after the anchor a leading window needs."""
    if output.key in seen:
        return 0
    if output.kind in {"window", "trend"} and (output.window_range or "trailing") == "leading":
        n = output.trailing_months or 1
        return max(n - 1, 0) if output.inclusive else n
    if output.kind in {"arithmetic", "fn", "expr"}:
        deeper = seen | {output.key}
        return max(
            (
                lookforward_for(by_key[n], by_key, seen=deeper)
                for n in _operand_keys(output)
                if n in by_key
            ),
            default=0,
        )
    return 0


def _operand_keys(output: OutputSpec) -> list[str]:
    """Measure keys this measure consumes (fn/expr inputs, arithmetic operands)."""
    if output.kind in {"fn", "expr"}:
        return list(output.inputs)
    if output.operands:
        return list(output.operands)
    return [n for n in (output.left, output.right) if n]


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
