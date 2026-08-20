"""Parse the immutable metadata context into an AdaptedRequest."""

from __future__ import annotations

from typing import Any

from kpi_engine.contracts import (
    AdaptedRequest,
    DatasetBinding,
    FilterMapping,
    IncomingFilter,
    Pagination,
)
from kpi_engine.exceptions import ContextError, FilterError


def adapt(context: dict[str, Any]) -> AdaptedRequest:
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

    measure_keys = _measure_keys(view.get("measures_required"))
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
    )


def _measure_keys(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContextError("measures_required must be a list.")
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or "measure_key" not in item:
            raise ContextError("Each measures_required entry needs measure_key.")
        key = item["measure_key"]
        if not isinstance(key, str) or not key:
            raise ContextError(f"Invalid measure_key: {key!r}.")
        keys.append(key)
    return tuple(keys)


def _filters(raw: Any) -> tuple[IncomingFilter, ...]:
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
    if values is None:
        return ()
    if isinstance(values, list):
        return tuple(values)
    return (values,)


def _datasets(raw: Any) -> tuple[DatasetBinding, ...]:
    if not isinstance(raw, dict) or not raw:
        raise ContextError("datasets must be a non-empty object.")
    out: list[DatasetBinding] = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise ContextError(f"datasets[{key!r}] must be an object.")
        path = spec.get("path")
        if not path:
            raise ContextError(f"datasets[{key!r}].path is required.")
        alias = spec.get("alias") or key
        columns = spec.get("columns") or []
        if not isinstance(columns, list):
            raise ContextError(f"datasets[{key!r}].columns must be a list.")
        mappings = tuple(_mapping(m) for m in spec.get("filter_column_mappings") or [])
        out.append(
            DatasetBinding(
                key=str(key),
                alias=str(alias),
                path=str(path),
                table_type=str(spec.get("table_type") or "PARQUET"),
                columns=tuple(str(c) for c in columns),
                mappings=mappings,
            )
        )
    return tuple(out)


def _mapping(raw: Any) -> FilterMapping:
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
    if not isinstance(raw, dict):
        return Pagination(page=None, page_size=None, limit=None)
    return Pagination(
        page=_optional_int(raw.get("page")),
        page_size=_optional_int(raw.get("page_size")),
        limit=_optional_int(raw.get("limit")),
    )


def _require_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContextError(f"{key} must be an object.")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
