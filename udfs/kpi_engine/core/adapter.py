"""Parse the immutable metadata context into AdaptedRequest.

What this file provides
    `adapt(context)` plus helpers to read execution, filters, datasets, pagination.

Where it is used
    orchestrator.compute / validate as the first step. Tests in test_adapter.py.

Capabilities
    - Requires execution.kpi_id and exactly one view_details entry.
    - Reads measures_required or measures_requested[].measure_key (projection).
    - Normalizes filter `value` or `values` to a list (default operator IN).
    - Rejects input_text=heir (hierarchy must be expanded upstream).
    - Ignores business_date (never used in calculations).
    - Dataset path may be omitted; binder fills model default_path when present.
    - Does not claim the month filter; time_planner does that later.

When to use
    Touch this file when the context JSON shape changes (new required fields,
    filter formats). Do not put KPI YAML logic here.
"""

from __future__ import annotations

from typing import Any

from kpi_engine.contracts import (
    GRAIN_NAMES,
    AdaptedRequest,
    DatasetBinding,
    FilterMapping,
    IncomingFilter,
    Pagination,
)
from kpi_engine.exceptions import ContextError, FilterError
from kpi_engine.identifiers import norm_name
from kpi_engine.runlog import traced


@traced
def adapt(context: dict[str, Any]) -> AdaptedRequest:
    """Parse the metadata context into a typed request. Does not load KPI YAML."""
    if not isinstance(context, dict):
        raise ContextError("Context must be a JSON object.")

    execution = _require_dict(context, "execution")
    kpi_id = execution.get("kpi_id")
    if kpi_id is None:
        raise ContextError("execution.kpi_id is required.")

    views = execution.get("view_details")
    if not isinstance(views, list) or len(views) != 1:
        raise ContextError(
            "execution.view_details must contain exactly one view "
            f"(got {0 if not isinstance(views, list) else len(views)})."
        )
    view = views[0]
    if not isinstance(view, dict):
        raise ContextError("execution.view_details[0] must be an object.")

    raw_measures, field = _measures_field(view, execution)
    measures_omitted = field is None or raw_measures is None
    measure_keys = () if measures_omitted else _measure_keys(raw_measures)
    filters = _filters(context.get("filters") or {})
    datasets = _datasets(context.get("datasets") or {})
    pagination = _pagination(context.get("output") or {})

    return AdaptedRequest(
        kpi_id=kpi_id,
        request_id=_optional_str(execution.get("request_id")),
        measure_keys=measure_keys,
        filters=filters,
        datasets=datasets,
        pagination=pagination,
        raw=context,
        time_grain=_time_grain(execution),
        measures_omitted=measures_omitted,
    )


def _measures_field(view: dict[str, Any], execution: dict[str, Any]) -> tuple[Any, str | None]:
    """Host list of requested measures: measures_required or measures_requested."""
    for owner in (view, execution):
        found, field = _lookup_present(owner, "measures_required", "measures_requested")
        if field is not None:
            return found, field
    return None, None


def _lookup_present(obj: dict[str, Any], *names: str) -> tuple[Any, str | None]:
    """Return (value, original_key) when the folded name is present, including []."""
    wanted = {norm_name(name).replace("_", "") for name in names}
    for key, value in obj.items():
        folded = norm_name(str(key)).replace("_", "")
        if folded in wanted:
            return value, str(key)
    return None, None


def _lookup(obj: dict[str, Any], *names: str) -> tuple[Any, str | None]:
    """Return (value, original_key) matching a name after case/space fold."""
    found, field = _lookup_present(obj, *names)
    if field is not None and found is not None and found != []:
        return found, field
    return None, None


def _time_grain(execution: dict[str, Any]) -> str | None:
    """Optional execution.time_grain pick; missing means YAML time.grain."""
    raw = execution.get("time_grain")
    if raw is None or raw == "":
        return None
    grain = str(raw)
    if grain not in GRAIN_NAMES:
        raise ContextError(
            "execution.time_grain must be day, week, month, quarter, or year "
            f"(got {grain!r})."
        )
    return grain


def _measure_keys(raw: Any) -> tuple[str, ...]:
    """Read measures_required / measures_requested[].measure_key from the view."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContextError("measures_required must be a list.")
    keys: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            keys.append(item)
            continue
        if not isinstance(item, dict):
            raise ContextError("Each measures_required entry needs measure_key.")
        key, found = _lookup(item, "measure_key")
        if found is None:
            raise ContextError("Each measures_required entry needs measure_key.")
        if not isinstance(key, str) or not key:
            raise ContextError(f"Invalid measure_key: {key!r}.")
        keys.append(key)
    return tuple(keys)


def _filters(raw: Any) -> tuple[IncomingFilter, ...]:
    """Normalize filters; accept value or values; reject hierarchical (heir) filters."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ContextError("filters must be an object.")
    out: list[IncomingFilter] = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise ContextError(f"filters[{key!r}] must be an object.")
        if spec.get("input_text") == "heir":
            raise FilterError(
                f"Filter {key!r} is hierarchical (input_text=heir). "
                "Expand leaf values in the context builder before calling the engine."
            )
        values = spec.get("values", spec.get("value"))
        out.append(
            IncomingFilter(
                raw_key=str(key),
                code=str(key),
                values=_as_list(values),
                input_text=_optional_str(spec.get("input_text")),
            )
        )
    return tuple(out)


def _as_list(values: Any) -> tuple[Any, ...]:
    """Turn a scalar or list into a tuple so every filter is IN (...)."""
    if values is None:
        return ()
    if isinstance(values, list):
        return tuple(values)
    return (values,)


def _datasets(raw: Any) -> tuple[DatasetBinding, ...]:
    """Bind context.datasets (alias, columns, mappings). Path may be filled later from YAML."""
    if not isinstance(raw, dict) or not raw:
        raise ContextError("datasets must be a non-empty object.")
    out: list[DatasetBinding] = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise ContextError(f"datasets[{key!r}] must be an object.")
        path = spec.get("path")
        alias = spec.get("alias") or key
        columns = spec.get("columns") or []
        if not isinstance(columns, list):
            raise ContextError(f"datasets[{key!r}].columns must be a list.")
        mappings = tuple(_mapping(m) for m in spec.get("filter_column_mappings") or [])
        out.append(
            DatasetBinding(
                key=str(key),
                alias=str(alias),
                path=str(path) if path else "",
                table_type=str(spec.get("table_type") or "PARQUET"),
                columns=tuple(str(c) for c in columns),
                mappings=mappings,
            )
        )
    return tuple(out)


def _mapping(raw: Any) -> FilterMapping:
    """Parse one filter_column_mappings entry (filter_code → column_name)."""
    if not isinstance(raw, dict):
        raise ContextError("filter_column_mappings entries must be objects.")
    code = raw.get("filter_code")
    column = raw.get("column_name")
    if not code or not column:
        raise ContextError("filter_column_mappings need filter_code and column_name.")
    return FilterMapping(
        filter_code=str(code),
        column_name=str(column),
        operator=str(raw.get("operator") or "in").lower(),
        view_id=raw.get("view_id"),
    )


def _pagination(raw: Any) -> Pagination:
    """Read output.page / page_size / limit. Missing values stay None."""
    if not isinstance(raw, dict):
        return Pagination(page=None, page_size=None, limit=None)
    return Pagination(
        page=_optional_int(raw.get("page")),
        page_size=_optional_int(raw.get("page_size")),
        limit=_optional_int(raw.get("limit")),
    )


def _require_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Return parent[key] or raise if it is missing or not an object."""
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContextError(f"{key} must be an object.")
    return value


def _optional_str(value: Any) -> str | None:
    """Coerce a value to str, or None when the field was omitted."""
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    """Coerce a value to int, or None when the field was omitted."""
    if value is None or value == "":
        return None
    return int(value)
