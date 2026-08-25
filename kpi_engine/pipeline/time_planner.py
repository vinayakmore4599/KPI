"""Anchor period and required_span. Time parts are predicates, never leftover INs.

What this file provides
    plan_time, claim_month_filter, max_lookback_months, lookback_for.
    apply_year_basis — host year part forces calendar Jan–Dec years.

Where it is used
    orchestrator after YAML bind. validate() uses it to report span_start
    without scanning.

Capabilities
    - Scalar time.filter_code on the context wins (legacy single-bucket).
    - Else time.periods parts conjoin; a missing part is not applied.
    - Year-bounded selections materialize S in-process; unbounded ones leave
      anchor None for a later data probe.
    - Snapshot KPIs with no time: block skip the claim.
    - Lookback from requested measures only, in KPI grain periods.
    - Gregorian or fiscal truncate via dates.truncate_period.
    - Returns remaining filters for IN binding (time filter and parts removed).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Mapping

from kpi_engine.contracts import (
    GRAIN_RANK,
    AdaptedRequest,
    IncomingFilter,
    KpiSpec,
    OutputSpec,
    TimePlan,
    TimeSelection,
    TimeSpec,
)
from kpi_engine.dates import (
    add_periods,
    apply_offset,
    parse_date,
    periods_between,
    truncate_period,
)
from kpi_engine.pipeline.binder import fold_measure_keys
from kpi_engine.pipeline.compose import expand_compose, strip_compose_keys
from kpi_engine.pipeline.period_select import (
    empty_reason,
    negate_offset,
    parts_as_tuple,
    read_period_parts,
    selection_bounds,
    selection_periods,
    shift_selection,
    strip_period_keys,
)
from kpi_engine.exceptions import BindError, TimePlanError
from kpi_engine.runlog import traced


def apply_year_basis(
    kpi: KpiSpec,
    *,
    parts: Mapping[str, Any] | None = None,
    time_plan: TimePlan | None = None,
) -> KpiSpec:
    """Force Jan–Dec years when the host sent a year part. Quarters stay fiscal."""
    if kpi.time is None:
        return kpi
    has_year = False
    if parts and "year" in parts and parts["year"]:
        has_year = True
    elif time_plan is not None and time_plan.selection is not None:
        has_year = any(
            name == "year" and values for name, values in time_plan.selection.parts
        )
    if not has_year or kpi.time.year_basis == "calendar":
        return kpi
    return replace(kpi, time=replace(kpi.time, year_basis="calendar"))


@traced
def plan_time(request: AdaptedRequest, kpi: KpiSpec) -> tuple[TimePlan | None, tuple[IncomingFilter, ...]]:
    """Claim the time filter as a selection, compute required_span, return remaining filters.

    Snapshot KPIs (no YAML time:) skip the claim and leave every filter for IN binding.
    """
    if kpi.time is None:
        return None, request.filters
    keys = fold_measure_keys(kpi, request.measure_keys)
    claimed, rest = claim_month_filter(request.filters, kpi.time.filter_code)
    template = kpi.time.compose_template
    if claimed is not None and kpi.time.filter_code:
        return _legacy_scalar_plan(kpi, claimed, rest, keys, template)

    if kpi.time.periods:
        parts, rest = read_period_parts(request.filters, kpi.time.periods)
        kpi = apply_year_basis(kpi, parts=parts)
        return _parts_plan(kpi, parts, rest, keys), rest

    if template:
        try:
            composed, _consumed = expand_compose(template, request.filters, what="time.compose.template")
        except BindError as exc:
            raise TimePlanError(str(exc)) from exc
        rest = strip_compose_keys(rest if claimed is None else rest, template)
        claimed = IncomingFilter(
            raw_key=kpi.time.filter_code or "composed",
            code=kpi.time.filter_code or "composed",
            values=(composed,),
            input_text=None,
        )
        return _legacy_scalar_plan(kpi, claimed, rest, keys, template=None)

    # No scalar, no parts, no compose: whole-history (probe later).
    return _unbounded_plan(kpi, {}, keys), request.filters


def _legacy_scalar_plan(
    kpi: KpiSpec,
    claimed: IncomingFilter,
    rest: tuple[IncomingFilter, ...],
    keys: tuple[str, ...],
    template: str | None,
) -> tuple[TimePlan, tuple[IncomingFilter, ...]]:
    """Existing single-value filter_code / compose path. Numbers stay byte-identical."""
    if len(claimed.values) != 1:
        raise TimePlanError(
            f"Month filter {claimed.code!r} must contain exactly one value "
            f"(got {len(claimed.values)})."
        )
    if template:
        rest = strip_compose_keys(rest, template)
    rest = strip_period_keys(rest, kpi.time.periods)
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
    selection = TimeSelection(
        parts=(),
        start=anchor,
        end=anchor,
        periods=(anchor,),
        anchor_source="legacy",
    )
    plan = TimePlan(
        anchor=anchor,
        span_start=anchor,
        span_end_exclusive=add_periods(anchor, 1, kpi.time),
        lookback_months=0,
        claimed_filter_code=claimed.code,
        lookback_forward=0,
        selection=selection,
    )
    return span_for_keys(kpi, keys, plan), rest


def _parts_plan(
    kpi: KpiSpec,
    parts: dict[str, tuple[int, ...]],
    rest: tuple[IncomingFilter, ...],
    keys: tuple[str, ...],
) -> TimePlan:
    """Independent year/month/… predicates. Year-bounded plans fill S now."""
    del rest
    bounds = selection_bounds(parts, kpi.time)
    if bounds is None:
        return _unbounded_plan(kpi, parts, keys)
    periods = selection_periods(parts, bounds, kpi.time)
    if not periods:
        return TimePlan(
            anchor=None,
            span_start=None,
            span_end_exclusive=None,
            lookback_months=max_lookback_months(kpi, keys),
            claimed_filter_code="",
            selection=TimeSelection(
                parts=parts_as_tuple(parts),
                start=None,
                end=None,
                periods=(),
                anchor_source="context",
                empty_reason=empty_reason(parts),
            ),
        )
    selection = TimeSelection(
        parts=parts_as_tuple(parts),
        start=periods[0],
        end=periods[-1],
        periods=periods,
        anchor_source="context",
    )
    plan = TimePlan(
        anchor=periods[-1],
        span_start=periods[0],
        span_end_exclusive=add_periods(periods[-1], 1, kpi.time),
        lookback_months=0,
        claimed_filter_code="",
        selection=selection,
    )
    return span_for_keys(kpi, keys, plan)


def _unbounded_plan(
    kpi: KpiSpec, parts: dict[str, tuple[int, ...]], keys: tuple[str, ...]
) -> TimePlan:
    """No year (or no parts): probe the data for min/max after filters bind."""
    return TimePlan(
        anchor=None,
        span_start=None,
        span_end_exclusive=None,
        lookback_months=max_lookback_months(kpi, keys),
        claimed_filter_code="",
        lookback_forward=max_lookforward_periods(kpi, keys),
        selection=TimeSelection(
            parts=parts_as_tuple(parts),
            start=None,
            end=None,
            periods=(),
            anchor_source="data",
        ),
    )


def apply_request_time(kpi: KpiSpec, time_grain: str | None = None) -> KpiSpec:
    """Pick the request grain, reject finer-than-source, resolve data_points trailing."""
    if kpi.time is None:
        return kpi
    allowed = kpi.time.grains or (kpi.time.grain,)
    pick = time_grain or kpi.time.grain
    if pick not in allowed:
        raise BindError(
            f"parameters.time_grain {pick!r} is not allowed "
            f"(time.grains {list(allowed)})."
        )
    source = kpi.time.source_grain or kpi.time.grain
    if GRAIN_RANK[pick] < GRAIN_RANK[source]:
        raise BindError(
            f"parameters.time_grain {pick!r} is finer than time.source_grain {source!r}."
        )
    time = replace(kpi.time, grain=pick)
    measures = []
    for spec in kpi.measures:
        if spec.trailing_from != "data_points":
            measures.append(spec)
            continue
        n = _data_points_for(kpi, pick)
        measures.append(replace(spec, trailing_months=n, trailing_unit=None))
    return replace(kpi, time=time, measures=tuple(measures))


def _data_points_for(kpi: KpiSpec, grain: str) -> int:
    """Positive length from YAML data_points for this effective grain."""
    raw = kpi.data_points
    if raw is None:
        raise TimePlanError("trailing.from: data_points needs a top-level data_points:.")
    if isinstance(raw, int):
        return raw
    if grain not in raw:
        raise TimePlanError(
            f"data_points has no entry for grain {grain!r} (have {sorted(raw)})."
        )
    return int(raw[grain])


def span_for_keys(kpi: KpiSpec, keys: tuple[str, ...], plan: TimePlan | None) -> TimePlan | None:
    """Widen or shrink an already-claimed plan to this pipeline's measure keys."""
    if plan is None or kpi.time is None or plan.anchor is None:
        return plan
    lookback = max_lookback_months(kpi, keys, anchor=plan.anchor)
    forward = max_lookforward_periods(kpi, keys, anchor=plan.anchor)
    from_anchor = add_periods(plan.anchor, -lookback, kpi.time)
    periods = plan.selection.periods if plan.selection and plan.selection.periods else (plan.anchor,)
    earliest = min(periods)
    shifted = _earliest_shifted(kpi, keys, periods)
    span_start = min(from_anchor, earliest)
    if shifted is not None:
        span_start = min(span_start, shifted)
    return replace(
        plan,
        span_start=span_start,
        span_end_exclusive=add_periods(plan.anchor, 1 + forward, kpi.time),
        lookback_months=lookback,
        lookback_forward=forward,
    )


def fill_probed_selection(
    kpi: KpiSpec,
    plan: TimePlan,
    lo: date,
    hi: date,
    keys: tuple[str, ...],
) -> TimePlan:
    """Materialize S from a min/max probe and compute span. Empty if lo/hi missing."""
    parts = dict(plan.selection.parts) if plan.selection else {}
    parsed = {name: tuple(int(v) for v in values) for name, values in parts.items()}
    periods = selection_periods(parsed, (lo, hi), kpi.time)
    if not periods:
        return replace(
            plan,
            anchor=None,
            span_start=None,
            span_end_exclusive=None,
            selection=replace(
                plan.selection or TimeSelection(anchor_source="data"),
                periods=(),
                start=None,
                end=None,
                empty_reason=empty_reason(parsed),
            ),
        )
    selection = TimeSelection(
        parts=parts_as_tuple(parsed),
        start=periods[0],
        end=periods[-1],
        periods=periods,
        anchor_source="data",
    )
    filled = replace(
        plan,
        anchor=periods[-1],
        span_start=periods[0],
        span_end_exclusive=add_periods(periods[-1], 1, kpi.time),
        selection=selection,
    )
    return span_for_keys(kpi, keys, filled) or filled


def _earliest_shifted(kpi: KpiSpec, keys: tuple[str, ...], periods: tuple[date, ...]) -> date | None:
    """Earliest bucket of any requested offset measure's shifted selection."""
    if kpi.time is None or not periods:
        return None
    earliest: date | None = None
    by_key = {m.key: m for m in kpi.measures}
    for key in keys:
        spec = by_key.get(key)
        if spec is None or spec.offset is None:
            continue
        shifted = shift_selection(periods, negate_offset(spec.offset), kpi.time)
        if not shifted:
            continue
        low = min(shifted)
        earliest = low if earliest is None else min(earliest, low)
    return earliest


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


def max_lookforward_periods(kpi: KpiSpec, requested: tuple[str, ...], anchor=None) -> int:
    """Periods after the anchor needed for leading or full-period windows."""
    keys = fold_measure_keys(kpi, requested)
    by_key = {m.key: m for m in kpi.measures}
    return max(
        (
            lookforward_for(by_key[k], by_key, time=kpi.time, anchor=anchor)
            for k in keys
            if k in by_key
        ),
        default=0,
    )


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
    from kpi_engine.pipeline.op_registry import get_op
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
    time: TimeSpec | None = None,
    anchor=None,
) -> int:
    """How many grain periods after the anchor a leading or full-period window needs."""
    if output.key in seen:
        return 0
    from kpi_engine.pipeline.op_registry import get_op

    def _walk(child, by_key, seen=frozenset()):
        return lookforward_for(child, by_key, seen=seen, time=time, anchor=anchor)

    return get_op(output.kind).lookforward(
        output, by_key, seen, _walk, time=time, anchor=anchor
    )


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
