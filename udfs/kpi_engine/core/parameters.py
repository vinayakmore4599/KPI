"""Bind ``context.parameters`` against a KPI YAML schema.

Schema bind happens **before** ``when:`` / ``from_param:`` resolve so request
values pick YAML bodies. After ``_parse_kpi``, only stash + reserved overlays
run (``apply_request_time``, ``locked_cut``).

Reserved names:

- ``time_grain`` — same overlay as the old ``execution.time_grain`` (v1)
- ``output_cut`` — emit that cut only; drop ``also_emit``

User names inject into measure expr / fn kwargs. Collision with a YAML
``measures:`` **key** is a bind error (keys are static; ``when:`` only
switches bodies).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from kpi_engine.contracts import (
    AdaptedRequest,
    BoundParameters,
    KpiSpec,
    ParameterSpec,
    TimeSpec,
)
from kpi_engine.exceptions import BindError
from kpi_engine.core.time_planner import apply_request_time
from kpi_engine.runlog import log_step, traced

RESERVED_TIME_GRAIN = "time_grain"
RESERVED_OUTPUT_CUT = "output_cut"
RESERVED_PARAMETER_NAMES = frozenset({RESERVED_TIME_GRAIN, RESERVED_OUTPUT_CUT})
RESERVED_CASE_LABELS = frozenset({"param", "cases", "else"})

SCALAR_TYPES = frozenset({"string", "int", "float", "bool"})
COLLECTION_TYPES = frozenset({"list", "dict"})
ITEM_TYPES = frozenset({"string", "int", "float", "bool"})


def bind_incoming(
    incoming: dict[str, Any] | None,
    schema: tuple[ParameterSpec, ...],
    *,
    time: TimeSpec | None,
    cut_names: tuple[str, ...],
    measure_keys: tuple[str, ...],
    kpi_id: int | str,
) -> BoundParameters:
    """Coerce request parameters against the KPI schema (no ``KpiSpec`` yet)."""
    incoming = dict(incoming or {})
    if not schema:
        if incoming:
            raise BindError(
                "This KPI declares no parameters; "
                f"{sorted(incoming)} must not be sent in context.parameters. "
                "Add a YAML parameters: block, or omit them."
            )
        return BoundParameters()

    declared = {spec.name: spec for spec in schema}
    unknown = [key for key in incoming if key not in declared]
    if unknown:
        raise BindError(
            f"Unknown parameter(s) {unknown}. Declared: {sorted(declared)}."
        )

    values: dict[str, Any] = {}
    for spec in schema:
        if spec.name in incoming:
            values[spec.name] = _finalize(spec, incoming[spec.name], time)
        elif spec.has_default:
            values[spec.name] = _finalize(spec, spec.default, time)
        elif spec.name == RESERVED_TIME_GRAIN and time is not None:
            values[spec.name] = _finalize(spec, time.grain, time)
        else:
            raise BindError(f"Missing required parameter {spec.name!r}.")
        _reject_reserved_case_label(spec.name, values[spec.name])

    locked_cut = None
    if RESERVED_OUTPUT_CUT in declared:
        locked_cut = str(values[RESERVED_OUTPUT_CUT])
        if locked_cut not in cut_names:
            raise BindError(
                f"parameters.output_cut {locked_cut!r} is not a declared cut "
                f"(have {sorted(cut_names)})."
            )

    if RESERVED_TIME_GRAIN in declared:
        grain = values[RESERVED_TIME_GRAIN]
        if not isinstance(grain, str):
            raise BindError(
                f"Reserved parameter {RESERVED_TIME_GRAIN!r} must be a string "
                f"grain name, got {grain!r}."
            )

    clash = sorted(set(values) & set(measure_keys))
    if clash:
        raise BindError(
            f"Parameter name(s) {clash} collide with measure keys. "
            "Rename the parameter."
        )

    log_step("bind_parameters", request_parameters=values)
    return BoundParameters(values=values, schema=schema, locked_cut=locked_cut)


def apply_bound_to_spec(kpi: KpiSpec, bound: BoundParameters) -> KpiSpec:
    """Stamp bound values and reserved overlays onto a parsed KPI."""
    kpi = replace(
        kpi,
        bound_parameters=dict(bound.values),
        locked_cut=bound.locked_cut,
        model_templated=bound.model_templated,
    )
    if RESERVED_TIME_GRAIN in bound.values:
        kpi = apply_request_time(kpi, bound.values[RESERVED_TIME_GRAIN])
    return kpi


@traced
def bind_parameters(request: AdaptedRequest, kpi: KpiSpec) -> KpiSpec:
    """Bind after parse (tests / leftover callers). Prefer ``load_kpi(..., parameters=)``."""
    bound = bind_incoming(
        dict(request.parameters),
        kpi.parameter_schema,
        time=kpi.time,
        cut_names=tuple(c.name for c in kpi.cuts),
        measure_keys=tuple(m.key for m in kpi.measures),
        kpi_id=kpi.kpi_id,
    )
    return apply_bound_to_spec(
        kpi,
        replace(bound, model_templated=kpi.model_templated),
    )


def declared_time_grain(kpi: KpiSpec) -> str | None:
    """Bound time_grain when the KPI declares that parameter; else None."""
    if not any(spec.name == RESERVED_TIME_GRAIN for spec in kpi.parameter_schema):
        return None
    grain = kpi.bound_parameters.get(RESERVED_TIME_GRAIN)
    return None if grain is None else str(grain)


def schema_types(schema: tuple[ParameterSpec, ...]) -> dict[str, str]:
    return {p.name: p.type_name for p in schema}


def _finalize(spec: ParameterSpec, raw: Any, time: TimeSpec | None) -> Any:
    """Map alias → canonical, coerce type, check allowed."""
    if spec.type_name == "list":
        if not isinstance(raw, list):
            raise BindError(
                f"parameters.{spec.name} must be a list (got {raw!r})."
            )
        out = [_finalize_item(spec, el, i) for i, el in enumerate(raw)]
        return out
    if spec.type_name == "dict":
        return _coerce_dict(spec, raw)
    if isinstance(raw, (list, dict)):
        raise BindError(
            f"parameters.{spec.name} must be a {spec.type_name} "
            f"(got a list or object)."
        )
    value = _apply_map(spec, raw)
    value = _coerce_scalar(spec, value)
    allowed = _allowed(spec, time)
    if allowed is not None and value not in allowed:
        raise BindError(
            f"parameters.{spec.name} value {value!r} is not allowed "
            f"(allowed {list(allowed)})."
        )
    return value


def _finalize_item(spec: ParameterSpec, raw: Any, index: int) -> Any:
    if raw is None:
        raise BindError(
            f"parameters.{spec.name}[{index}] is null; list items cannot be null."
        )
    item = ParameterSpec(
        name=f"{spec.name}[{index}]",
        type_name=spec.item_type or "string",
        value_map=spec.value_map,
        allowed=spec.allowed,
    )
    value = _apply_map(spec, raw)
    value = _coerce_scalar(item, value)
    if spec.allowed is not None and value not in spec.allowed:
        raise BindError(
            f"parameters.{spec.name}[{index}] value {value!r} is not allowed "
            f"(allowed {list(spec.allowed)})."
        )
    return value


def _coerce_dict(spec: ParameterSpec, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BindError(
            f"parameters.{spec.name} must be an object (got {raw!r})."
        )
    item_t = spec.item_type or "string"
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise BindError(
                f"parameters.{spec.name} keys must be strings."
            )
        if value is None or isinstance(value, (list, dict)):
            raise BindError(
                f"parameters.{spec.name}[{key}] must be a scalar; "
                "nested objects/lists are not allowed."
            )
        item = ParameterSpec(name=f"{spec.name}[{key}]", type_name=item_t)
        out[key] = _coerce_scalar(item, value)
    return out


def _apply_map(spec: ParameterSpec, raw: Any) -> Any:
    """Rewrite an alias (Green → G) when YAML declares map:."""
    mapping = spec.value_map
    if not mapping:
        return raw
    if raw in mapping:
        return mapping[raw]
    as_str = str(raw)
    if as_str in mapping:
        return mapping[as_str]
    return raw


def _coerce_scalar(spec: ParameterSpec, raw: Any) -> Any:
    """Require the JSON/YAML value to match the declared scalar type."""
    name = spec.name
    wanted = spec.type_name
    if wanted == "string":
        if not isinstance(raw, str):
            raise BindError(
                f"parameters.{name} must be a string (got {raw!r})."
            )
        return raw
    if wanted == "bool":
        if not isinstance(raw, bool):
            raise BindError(
                f"parameters.{name} must be a bool (got {raw!r})."
            )
        return raw
    if wanted == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BindError(
                f"parameters.{name} must be an int (got {raw!r})."
            )
        return raw
    if wanted == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BindError(
                f"parameters.{name} must be a float (got {raw!r})."
            )
        return float(raw)
    raise BindError(
        f"parameters.{name}.type {wanted!r} is not string, int, float, bool, list, or dict."
    )


def _allowed(spec: ParameterSpec, time: TimeSpec | None) -> tuple[Any, ...] | None:
    """Explicit allowed:, else time.grains for the reserved time_grain param."""
    if spec.allowed is not None:
        return spec.allowed
    if spec.name == RESERVED_TIME_GRAIN and time is not None:
        return time.grains or (time.grain,)
    return None


def _reject_reserved_case_label(name: str, value: Any) -> None:
    if isinstance(value, dict):
        return
    if isinstance(value, list):
        for item in value:
            if item in RESERVED_CASE_LABELS:
                raise BindError(
                    f"Parameter {name!r} resolved to {item!r}, which is "
                    "reserved as a when: metadata key (not a case label)."
                )
        return
    if value in RESERVED_CASE_LABELS:
        raise BindError(
            f"Parameter {name!r} resolved to {value!r}, which is "
            "reserved as a when: metadata key (not a case label)."
        )
