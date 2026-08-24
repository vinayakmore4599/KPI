"""Bind context.parameters to the KPI YAML schema.

What this file provides
    bind_parameters — type, default, map, allowed, reserved overlays.

Where it is used
    orchestrator after load_kpi, before apply_request_time / bind_filters.

Capabilities
    Scalars only (string, int, float, bool). Reserved names: time_grain
    (feeds apply_request_time) and output_cut (emit that cut only).
    A KPI with no parameters: block rejects a non-empty context.parameters.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from kpi_engine.contracts import AdaptedRequest, KpiSpec, ParameterSpec
from kpi_engine.exceptions import BindError
from kpi_engine.runlog import log_step, traced

RESERVED_TIME_GRAIN = "time_grain"
RESERVED_OUTPUT_CUT = "output_cut"
SCALAR_TYPES = frozenset({"string", "int", "float", "bool"})


@traced
def bind_parameters(request: AdaptedRequest, kpi: KpiSpec) -> KpiSpec:
    """Type-check context.parameters, apply defaults, stash values on the spec."""
    incoming = dict(request.parameters)
    schema = kpi.parameter_schema
    if not schema:
        if incoming:
            raise BindError(
                "This KPI declares no parameters; "
                f"{sorted(incoming)} must not be sent in context.parameters. "
                "Add a YAML parameters: block, or omit them."
            )
        return kpi

    declared = {spec.name: spec for spec in schema}
    unknown = [key for key in incoming if key not in declared]
    if unknown:
        raise BindError(
            f"Unknown parameter(s) {unknown}. Declared: {sorted(declared)}."
        )

    values: dict[str, Any] = {}
    for spec in schema:
        if spec.name in incoming:
            values[spec.name] = _finalize(spec, incoming[spec.name], kpi)
        elif spec.has_default:
            values[spec.name] = _finalize(spec, spec.default, kpi)
        elif spec.name == RESERVED_TIME_GRAIN and kpi.time is not None:
            values[spec.name] = _finalize(spec, kpi.time.grain, kpi)
        else:
            raise BindError(
                f"Missing required parameter {spec.name!r}."
            )

    clash = sorted(set(values) & {m.key for m in kpi.measures})
    if clash:
        raise BindError(
            f"Parameter name(s) {clash} collide with measure keys. "
            "Rename the parameter."
        )

    locked_cut = None
    if RESERVED_OUTPUT_CUT in declared:
        locked_cut = str(values[RESERVED_OUTPUT_CUT])
        known = {cut.name for cut in kpi.cuts}
        if locked_cut not in known:
            raise BindError(
                f"parameters.output_cut {locked_cut!r} is not a declared cut "
                f"(have {sorted(known)})."
            )

    log_step("bind_parameters", request_parameters=values)
    return replace(kpi, bound_parameters=values, locked_cut=locked_cut)


def declared_time_grain(kpi: KpiSpec) -> str | None:
    """Bound time_grain when the KPI declares that parameter; else None."""
    if not any(spec.name == RESERVED_TIME_GRAIN for spec in kpi.parameter_schema):
        return None
    grain = kpi.bound_parameters.get(RESERVED_TIME_GRAIN)
    return None if grain is None else str(grain)


def _finalize(spec: ParameterSpec, raw: Any, kpi: KpiSpec) -> Any:
    """Map alias → canonical, coerce type, check allowed."""
    value = _apply_map(spec, raw)
    value = _coerce(spec, value)
    allowed = _allowed(spec, kpi)
    if allowed is not None and value not in allowed:
        raise BindError(
            f"parameters.{spec.name} value {value!r} is not allowed "
            f"(allowed {list(allowed)})."
        )
    return value


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


def _coerce(spec: ParameterSpec, raw: Any) -> Any:
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
    raise BindError(f"parameters.{name}.type {wanted!r} is not string, int, float, or bool.")


def _allowed(spec: ParameterSpec, kpi: KpiSpec) -> tuple[Any, ...] | None:
    """Explicit allowed:, else time.grains for the reserved time_grain param."""
    if spec.allowed is not None:
        return spec.allowed
    if spec.name == RESERVED_TIME_GRAIN and kpi.time is not None:
        return kpi.time.grains or (kpi.time.grain,)
    return None
