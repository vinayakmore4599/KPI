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
    - Returns remaining filters for IN binding (time filter and compose parts removed).
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
from kpi_engine.core.compose import expand_compose, strip_compose_keys
from kpi_engine.exceptions import BindError, TimePlanError
from kpi_engine.runlog import traced


@traced
def plan_time(request: AdaptedRequest, kpi: KpiSpec) -> tuple[TimePlan | None, tuple[IncomingFilter, ...]]:
    """Claim the time filter as anchor, compute required_span, return remaining filters.

    Snapshot KPIs (no YAML time:) skip the claim and leave every filter for IN binding.
    """
    if kpi.time is None:
        return None, request.filters
    claimed, rest = claim_month_filter(request.filters, kpi.time.filter_code)
    template = kpi.time.compose_template
    if claimed is None:
        if not template:
            raise TimePlanError(
                f"Missing month filter {kpi.time.filter_code!r}. "
                "Set time.filter_code in the KPI YAML to the context filter that "
                "carries the selected period, set time.compose.template to build it "
                "from year/month keys, or omit the time: block if this KPI "
                "has no time column. The selected period is not defaulted."
            )
        try:
            composed, _consumed = expand_compose(template, request.filters, what="time.compose.template")
        except BindError as exc:
            raise TimePlanError(str(exc)) from exc
        claimed = IncomingFilter(
            raw_key=kpi.time.filter_code,
            code=kpi.time.filter_code,
            values=(composed,),
            input_text=None,
        )
    elif len(claimed.values) != 1:
        raise TimePlanError(
            f"Month filter {claimed.code!r} must contain exactly one value "
            f"(got {len(claimed.values)})."
        )
    if template:
        rest = strip_compose_keys(rest, template)
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
    from kpi_engine.core.op_registry import get_op
    from kpi_engine.exceptions import CatalogError

    try:
        plugin = get_op(output.kind)
    except CatalogError as exc:
        raise TimePlanError(
            f"Cannot plan lookback for {output.key} kind={output.kind}."
        ) from exc
    return plugin.lookback(output, by_key, time, anchor, seen, lookback_for)


def lookforward_for(
    output: OutputSpec,
    by_key: dict[str, OutputSpec],
    seen: frozenset[str] = frozenset(),
) -> int:
    """How many grain periods after the anchor a leading window needs."""
    if output.key in seen:
        return 0
    from kpi_engine.core.op_registry import get_op

    return get_op(output.kind).lookforward(output, by_key, seen, lookforward_for)


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
