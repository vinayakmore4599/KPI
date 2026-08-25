"""Independent time-part predicates → materialized selection of grain buckets.

What this file provides
    parse_part_values — int-parse year/quarter/month/week/day (3 and 03).
    selection_bounds — calendar span when a year part is present.
    selection_periods — dense grain buckets matching every provided part.
    shift_selection — apply a calendar offset to each selected bucket.
    period_matches / period_predicate_sql — Pandas vs DuckDB of the same mask.

Where it is used
    time_planner (context-bounded selections). model_sql probe. combo.Point
    and period ops when folding a multi-bucket selection.

Capabilities
    Parts conjoin; a missing part is not applied. Fiscal year/quarter follow
    time.fiscal_start_month. Week is ISO-only. Offsets shift each element
    (DAX SAMEPERIODLASTYEAR), never re-apply the original mask.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from kpi_engine.contracts import (
    GRAIN_RANK,
    PERIOD_PART_NAMES,
    IncomingFilter,
    Offset,
    TimeSpec,
)
from kpi_engine.dates import (
    add_days,
    add_months,
    apply_offset,
    period_range_inclusive,
    quarter_start,
    truncate_period,
    year_start,
)
from kpi_engine.exceptions import TimePlanError
from kpi_engine.identifiers import norm_name


_PART_LIMITS: dict[str, tuple[int, int]] = {
    "quarter": (1, 4),
    "month": (1, 12),
    "week": (1, 53),
    "day": (1, 31),
}


def parse_part_values(part: str, values: tuple[Any, ...]) -> tuple[int, ...]:
    """Parse one time part's context values as integers (3 and '03' both work)."""
    if part not in PERIOD_PART_NAMES:
        raise TimePlanError(
            f"Unknown time part {part!r}. Use year, quarter, month, week, or day."
        )
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        if raw is None or raw == "":
            continue
        number = _as_int(raw, part)
        lo_hi = _PART_LIMITS.get(part)
        if lo_hi is not None and not lo_hi[0] <= number <= lo_hi[1]:
            raise TimePlanError(
                f"time part {part} value {raw!r} is out of range "
                f"{lo_hi[0]}-{lo_hi[1]}."
            )
        if part == "year" and not (1 <= number <= 9999):
            raise TimePlanError(f"time part year value {raw!r} is not a valid year.")
        if number not in seen:
            out.append(number)
            seen.add(number)
    return tuple(out)


def read_period_parts(
    filters: tuple[IncomingFilter, ...],
    periods: tuple[tuple[str, str], ...],
) -> tuple[dict[str, tuple[int, ...]], tuple[IncomingFilter, ...]]:
    """Claim declared period filters; return parsed parts and leftover filters."""
    wanted = {part: _norm(code) for part, code in periods}
    claimed: dict[str, IncomingFilter] = {}
    rest: list[IncomingFilter] = []
    for item in filters:
        matched: str | None = None
        for part, code in wanted.items():
            if part in claimed:
                continue
            if _norm(item.code) == code or _norm(item.raw_key) == code:
                matched = part
                break
        if matched is None:
            rest.append(item)
            continue
        claimed[matched] = item
    parts: dict[str, tuple[int, ...]] = {}
    for part, item in claimed.items():
        parsed = parse_part_values(part, item.values)
        if parsed:
            parts[part] = parsed
    return parts, tuple(rest)


def strip_period_keys(
    filters: tuple[IncomingFilter, ...],
    periods: tuple[tuple[str, str], ...],
) -> tuple[IncomingFilter, ...]:
    """Drop declared period-part keys so they are not leftover IN filters."""
    wanted = {_norm(code) for _part, code in periods}
    if not wanted:
        return filters
    return tuple(
        item
        for item in filters
        if _norm(item.code) not in wanted and _norm(item.raw_key) not in wanted
    )


def selection_bounds(
    parts: Mapping[str, tuple[int, ...]], time: TimeSpec
) -> tuple[date, date] | None:
    """Inclusive calendar span covering the year part. None if no year (unbounded)."""
    years = parts.get("year")
    if not years:
        return None
    starts: list[date] = []
    ends: list[date] = []
    for year in years:
        start, end = _year_span(year, time)
        starts.append(start)
        ends.append(end)
    return min(starts), max(ends)


def selection_periods(
    parts: Mapping[str, tuple[int, ...]],
    bounds: tuple[date, date] | None,
    time: TimeSpec,
) -> tuple[date, ...]:
    """Materialize grain buckets in `bounds` that match every provided part."""
    if bounds is None:
        return ()
    start = truncate_period(bounds[0], time)
    end = truncate_period(bounds[1], time)
    return tuple(
        bucket
        for bucket in period_range_inclusive(start, end, time)
        if period_matches(bucket, parts, time)
    )


def period_matches(
    bucket: date, parts: Mapping[str, tuple[int, ...]], time: TimeSpec
) -> bool:
    """True when `bucket` satisfies every provided part (missing parts ignored)."""
    bucket = truncate_period(bucket, time)
    if "year" in parts:
        year = _bucket_year(bucket, time, iso=bool(parts.get("week")))
        if year not in parts["year"]:
            return False
    if "quarter" in parts and _bucket_quarter(bucket, time) not in parts["quarter"]:
        return False
    if "month" in parts and bucket.month not in parts["month"]:
        return False
    if "week" in parts and bucket.isocalendar().week not in parts["week"]:
        return False
    if "day" in parts and bucket.day not in parts["day"]:
        return False
    return True


def shift_selection(
    periods: tuple[date, ...], offset: Offset | None, time: TimeSpec
) -> tuple[date, ...]:
    """Shift each selected bucket by `offset` and re-truncate. Empty stays empty."""
    if not periods or offset is None:
        return periods
    out: list[date] = []
    seen: set[date] = set()
    for bucket in periods:
        shifted = truncate_period(apply_offset(bucket, offset), time)
        if shifted not in seen:
            out.append(shifted)
            seen.add(shifted)
    return tuple(sorted(out))


def negate_offset(offset: Offset | None) -> Offset | None:
    """Flip the sign of every calendar field."""
    if offset is None:
        return None
    from dataclasses import replace

    return replace(
        offset,
        days=-offset.days,
        months=-offset.months,
        years=-offset.years,
        quarters=-offset.quarters,
        weeks=-offset.weeks,
    )


def empty_reason(parts: Mapping[str, tuple[int, ...]]) -> str:
    """Human-readable explanation of which parts produced an empty selection."""
    if not parts:
        return "No periods in the data match the time selection."
    bits = ", ".join(f"{part}={list(values)}" for part, values in parts.items())
    return f"No periods match time parts {bits}."


def parts_as_tuple(
    parts: Mapping[str, tuple[int, ...]],
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    """Frozen form stored on TimeSelection."""
    return tuple((name, tuple(values)) for name, values in parts.items())


def parts_from_tuple(
    items: tuple[tuple[str, tuple[Any, ...]], ...],
) -> dict[str, tuple[int, ...]]:
    """Restore a mutable map from TimeSelection.parts."""
    return {name: tuple(int(v) for v in values) for name, values in items}


def period_predicate_sql(
    time_expr: str, parts: Mapping[str, tuple[int, ...]], time: TimeSpec
) -> tuple[str, list[Any]]:
    """DuckDB predicates equivalent to period_matches. Empty parts → TRUE."""
    clauses: list[str] = []
    params: list[Any] = []
    if "year" in parts:
        expr = _sql_year_expr(time_expr, time, iso=bool(parts.get("week")))
        clauses.append(f"{expr} IN ({_placeholders(parts['year'])})")
        params.extend(parts["year"])
    if "quarter" in parts:
        expr = _sql_quarter_expr(time_expr, time)
        clauses.append(f"{expr} IN ({_placeholders(parts['quarter'])})")
        params.extend(parts["quarter"])
    if "month" in parts:
        clauses.append(
            f"CAST(date_part('month', {time_expr}) AS INTEGER) "
            f"IN ({_placeholders(parts['month'])})"
        )
        params.extend(parts["month"])
    if "week" in parts:
        clauses.append(
            f"CAST(date_part('week', {time_expr}) AS INTEGER) "
            f"IN ({_placeholders(parts['week'])})"
        )
        params.extend(parts["week"])
    if "day" in parts:
        clauses.append(
            f"CAST(date_part('day', {time_expr}) AS INTEGER) "
            f"IN ({_placeholders(parts['day'])})"
        )
        params.extend(parts["day"])
    if not clauses:
        return "TRUE", params
    return " AND ".join(clauses), params


def assert_part_not_finer(part: str, grain: str) -> None:
    """Bind-time: a period part cannot be finer than the KPI grain."""
    from kpi_engine.exceptions import BindError

    if part not in GRAIN_RANK or grain not in GRAIN_RANK:
        return
    if GRAIN_RANK[part] < GRAIN_RANK[grain]:
        raise BindError(
            f"time.periods.{part} is finer than time.grain {grain!r}."
        )


def _as_int(raw: Any, part: str) -> int:
    """Coerce a context scalar to int; names like 'March' are rejected."""
    if isinstance(raw, bool) or isinstance(raw, float):
        raise TimePlanError(
            f"Cannot parse {part} {raw!r}. Use an integer (e.g. 3, 03)."
        )
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    raise TimePlanError(
        f"Cannot parse {part} {raw!r}. Use an integer (e.g. 3, 03)."
    )


def _year_span(year: int, time: TimeSpec) -> tuple[date, date]:
    """First and last calendar day of calendar/fiscal year `year`."""
    if time.calendar == "fiscal":
        start = date(year, time.fiscal_start_month, 1)
        last = add_days(add_months(start, 12), -1)
        return start, last
    return date(year, 1, 1), date(year, 12, 31)


def _bucket_year(bucket: date, time: TimeSpec, *, iso: bool) -> int:
    """Year number used for the year part: ISO, fiscal-start, or calendar."""
    if iso:
        return bucket.isocalendar().year
    if time.calendar == "fiscal":
        return year_start(bucket, time).year
    return bucket.year


def _bucket_quarter(bucket: date, time: TimeSpec) -> int:
    """1-4 quarter of `bucket` (fiscal when time.calendar is fiscal)."""
    start = quarter_start(bucket, time)
    if time.calendar == "fiscal":
        fy = year_start(bucket, time)
        months_in = (start.year - fy.year) * 12 + (start.month - fy.month)
        return months_in // 3 + 1
    return (start.month - 1) // 3 + 1


def _sql_year_expr(time_expr: str, time: TimeSpec, *, iso: bool) -> str:
    """DuckDB integer year expression matching `_bucket_year`."""
    if iso:
        return f"CAST(date_part('isoyear', {time_expr}) AS INTEGER)"
    if time.calendar == "fiscal":
        start = int(time.fiscal_start_month)
        return (
            f"CAST(CASE WHEN date_part('month', {time_expr}) >= {start} "
            f"THEN date_part('year', {time_expr}) "
            f"ELSE date_part('year', {time_expr}) - 1 END AS INTEGER)"
        )
    return f"CAST(date_part('year', {time_expr}) AS INTEGER)"


def _sql_quarter_expr(time_expr: str, time: TimeSpec) -> str:
    """DuckDB integer quarter expression matching `_bucket_quarter`."""
    if time.calendar != "fiscal":
        return f"CAST(date_part('quarter', {time_expr}) AS INTEGER)"
    start = int(time.fiscal_start_month)
    fy = (
        f"CASE WHEN date_part('month', {time_expr}) >= {start} "
        f"THEN date_part('year', {time_expr}) "
        f"ELSE date_part('year', {time_expr}) - 1 END"
    )
    return (
        f"CAST((("
        f"date_part('year', {time_expr}) - ({fy})"
        f") * 12 + (date_part('month', {time_expr}) - {start})"
        f") // 3 + 1 AS INTEGER)"
    )


def _placeholders(values: tuple[int, ...]) -> str:
    return ", ".join("?" for _ in values)


def _norm(value: str) -> str:
    return norm_name(value)
