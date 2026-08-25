"""Materialize ``when:`` / ``from_param:`` overlays before ``_parse_kpi``.

Resolve is a deepcopy no-op when those YAML **keys** are absent (3004 identity).
``from:`` is not a param overlay — it stays ``trailing.from`` / ``dimension.from``.

``when:`` is legal only on ``model``, ``measures.<key>``, ``base_measures.<name>``.
``from_param:`` is allowlisted (model, trailing/offset ints, constant ``value``).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from dataclasses import replace

from kpi_engine.contracts import BoundParameters, KpiSpec, ModelSpec
from kpi_engine.exceptions import BindError
from kpi_engine.core.parameters import RESERVED_CASE_LABELS
from kpi_engine.identifiers import norm_name

TRAILING_UNITS = frozenset({"months", "weeks", "days", "quarters", "years", "periods"})
OFFSET_UNITS = frozenset({"months", "weeks", "days", "quarters", "years"})


def yaml_has_overlays(raw: Mapping[str, Any]) -> bool:
    """True iff the YAML contains ``when:`` or ``from_param:`` keys (not expr text)."""
    return _contains_keys(raw, {"when", "from_param"})


def resolve_kpi(
    raw: Mapping[str, Any],
    bound: BoundParameters,
    *,
    force_else_for: str | None = None,
    force_case: tuple[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Pick ``when:`` bodies then substitute ``from_param:``.

    ``force_else_for`` / ``force_case`` are for all-case validation, not live
    requests. Returns ``(materialized_yaml, model_templated)``.
    """
    data = copy.deepcopy(dict(raw))
    model_templated = _model_is_templated(data.get("model"))
    _assert_when_slots(data)
    _assert_no_templated_model_plus_base_model(data)
    data = _resolve_when_tree(
        data,
        bound,
        force_else_for=force_else_for,
        force_case=force_case,
    )
    data = _subst_from_params(data, bound)
    return data, model_templated


def collect_when_param_names(raw: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    _walk_when(raw, names)
    # preserve order, unique
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


def validate_when_cases(
    raw: Mapping[str, Any],
    live: BoundParameters,
    *,
    parse_kpi: Callable[[Mapping[str, Any]], KpiSpec],
    load_model: Callable[[str], ModelSpec],
    check_columns: Callable[[KpiSpec, Mapping[str, ModelSpec]], None],
) -> None:
    """Parse and column-check every same-param ``when:`` case (and else).

    Two+ ``when.param`` names: each slot's cases in isolation (others at else)
    plus the live combination. Live parse always happens in ``load_kpi``.
    """
    names = collect_when_param_names(raw)
    if not names:
        return
    schema_by = {p.name: p for p in live.schema}
    if len(names) == 1:
        param = names[0]
        spec = schema_by.get(param)
        labels = list(spec.allowed) if spec is not None and spec.allowed else _case_labels(raw, param)
        seen: set[str] = set()
        for label in labels:
            key = str(label)
            if key in seen:
                continue
            seen.add(key)
            _parse_one_case(raw, live, param, case=key, parse_kpi=parse_kpi, load_model=load_model, check_columns=check_columns)
        _parse_one_case(
            raw, live, param, case=None, force_else=True,
            parse_kpi=parse_kpi, load_model=load_model, check_columns=check_columns,
        )
        return
    # Multi-param: isolate each when-slot's cases.
    for _path, when in _iter_when_slots(raw):
        param = str(when["param"])
        spec = schema_by.get(param)
        labels = list(spec.allowed) if spec is not None and spec.allowed else list((when.get("cases") or {}))
        for label in labels:
            _parse_one_case(
                raw, live, param, case=str(label),
                parse_kpi=parse_kpi, load_model=load_model, check_columns=check_columns,
            )
        _parse_one_case(
            raw, live, param, case=None, force_else=True,
            parse_kpi=parse_kpi, load_model=load_model, check_columns=check_columns,
        )


def _parse_one_case(
    raw: Mapping[str, Any],
    live: BoundParameters,
    param: str,
    *,
    case: str | None,
    force_else: bool = False,
    parse_kpi: Callable[[Mapping[str, Any]], KpiSpec],
    load_model: Callable[[str], ModelSpec],
    check_columns: Callable[[KpiSpec, Mapping[str, ModelSpec]], None],
) -> None:
    if force_else:
        materialized, templated = resolve_kpi(raw, live, force_else_for=param)
    else:
        materialized, templated = resolve_kpi(raw, live, force_case=(param, str(case)))
    kpi = parse_kpi(materialized)
    kpi = replace(
        kpi,
        model_templated=templated,
        bound_parameters=dict(live.values),
        locked_cut=live.locked_cut,
    )
    models: dict[str, ModelSpec] = {}
    for mid in _kpi_model_ids(kpi):
        models[norm_name(mid)] = load_model(mid)
        models[mid] = models[norm_name(mid)]
    check_columns(kpi, models)


def _kpi_model_ids(kpi: KpiSpec) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for measure in kpi.base_measures:
        mid = measure.model_id or kpi.model_id
        key = norm_name(mid)
        if key in seen:
            continue
        seen.add(key)
        ids.append(mid)
    if not ids and kpi.model_id:
        ids.append(kpi.model_id)
    return ids


def _case_labels(raw: Mapping[str, Any], param: str) -> list[str]:
    labels: list[str] = []
    for _path, when in _iter_when_slots(raw):
        if str(when.get("param")) != param:
            continue
        for key in when.get("cases") or {}:
            labels.append(str(key))
    return labels


def _walk_when(node: Any, names: list[str]) -> None:
    if isinstance(node, dict):
        if set(node.keys()) == {"when"} and isinstance(node.get("when"), dict):
            p = node["when"].get("param")
            if isinstance(p, str):
                names.append(p)
            for body in (node["when"].get("cases") or {}).values():
                _walk_when(body, names)
            _walk_when(node["when"].get("else"), names)
            return
        for v in node.values():
            _walk_when(v, names)
    elif isinstance(node, list):
        for v in node:
            _walk_when(v, names)


def _iter_when_slots(raw: Mapping[str, Any]) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    out: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    model = raw.get("model")
    if isinstance(model, dict) and "when" in model:
        out.append((("model",), model["when"]))
    measures = raw.get("measures") or {}
    if isinstance(measures, dict):
        for k, body in measures.items():
            if isinstance(body, dict) and "when" in body:
                out.append((("measures", str(k)), body["when"]))
    bases = raw.get("base_measures") or {}
    if isinstance(bases, dict):
        for k, body in bases.items():
            if isinstance(body, dict) and "when" in body:
                out.append((("base_measures", str(k)), body["when"]))
    return out


def _contains_keys(node: Any, keys: set[str]) -> bool:
    if isinstance(node, dict):
        if keys & set(node.keys()):
            return True
        return any(_contains_keys(v, keys) for v in node.values())
    if isinstance(node, list):
        return any(_contains_keys(v, keys) for v in node)
    return False


def _model_is_templated(model: Any) -> bool:
    if isinstance(model, dict) and ("when" in model or "from_param" in model):
        return True
    return False


def _path_str(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<root>"


def _allowed_when_path(path: tuple[str, ...]) -> bool:
    if path == ("model",):
        return True
    if len(path) == 2 and path[0] in {"measures", "base_measures"}:
        return True
    return False


def _assert_when_slots(raw: Mapping[str, Any]) -> None:
    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            if "when" in node:
                if not _allowed_when_path(path):
                    raise BindError(
                        f"when: is not allowed at {_path_str(path)}; "
                        f"only model, measures.<key>, and base_measures.<name>."
                    )
                extra = set(node) - {"when"}
                if extra:
                    raise BindError(
                        f"{_path_str(path)} mixes when: with other keys {sorted(extra)}; "
                        f"a slot is when: or from_param: or a concrete value, not two."
                    )
                _assert_when_object(node["when"], path)
                when = node["when"]
                for ck, body in (when.get("cases") or {}).items():
                    walk(body, path + ("cases", str(ck)))
                walk(when.get("else"), path + ("else",))
                return
            if _is_from_param(node) and "when" in node:
                raise BindError(
                    f"{_path_str(path)} mixes when: and from_param:."
                )
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + (str(i),))

    walk(dict(raw), ())


def _assert_when_object(when: Any, path: tuple[str, ...]) -> None:
    loc = _path_str(path)
    if not isinstance(when, dict):
        raise BindError(f"{loc} when: must be an object with param, cases, else.")
    extra = set(when) - {"param", "cases", "else"}
    if extra:
        raise BindError(
            f"{loc} when: has unknown keys {sorted(extra)}; "
            f"use param / cases / else (case labels live under cases:)."
        )
    missing = [k for k in ("param", "cases", "else") if k not in when]
    if missing:
        raise BindError(f"{loc} when: missing required {missing}.")
    param = when["param"]
    if not isinstance(param, str) or not param.strip():
        raise BindError(f"{loc} when.param must be a declared parameter name.")
    cases = when["cases"]
    if not isinstance(cases, dict) or not cases:
        raise BindError(f"{loc} when.cases must be a non-empty object.")
    reserved = sorted(set(cases) & RESERVED_CASE_LABELS)
    if reserved:
        raise BindError(
            f"{loc} when.cases keys {reserved} collide with when: metadata "
            f"(param / cases / else are not valid case labels)."
        )
    if when["else"] is None:
        raise BindError(f"{loc} when.else is required (exhaustive).")


def _assert_no_templated_model_plus_base_model(raw: Mapping[str, Any]) -> None:
    if not _model_is_templated(raw.get("model")):
        return
    bases = raw.get("base_measures") or {}
    if not isinstance(bases, dict):
        return
    named = [
        name
        for name, body in bases.items()
        if isinstance(body, dict) and "model" in body
    ]
    if named:
        raise BindError(
            "Top-level model is parameterized (when: / from_param:) but "
            f"base_measures {named} set model:; per-base model is not moved "
            "by the KPI default switch. Use when: on those bases, or drop "
            "their model: so they follow the chosen default."
        )


def _resolve_when_tree(
    node: Any,
    bound: BoundParameters,
    *,
    force_else_for: str | None,
    force_case: tuple[str, str] | None,
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, dict):
        if set(node.keys()) == {"when"}:
            return _pick_when(
                node["when"],
                bound,
                path,
                force_else_for=force_else_for,
                force_case=force_case,
            )
        return {
            k: _resolve_when_tree(
                v, bound,
                force_else_for=force_else_for,
                force_case=force_case,
                path=path + (str(k),),
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            _resolve_when_tree(
                v, bound,
                force_else_for=force_else_for,
                force_case=force_case,
                path=path + (str(i),),
            )
            for i, v in enumerate(node)
        ]
    return node


def _pick_when(
    when: Mapping[str, Any],
    bound: BoundParameters,
    path: tuple[str, ...],
    *,
    force_else_for: str | None,
    force_case: tuple[str, str] | None,
) -> Any:
    loc = _path_str(path)
    param = str(when["param"])
    if param not in bound.values:
        raise BindError(
            f"{loc} when.param {param!r} is not a declared parameter on this KPI."
        )
    spec = next((p for p in bound.schema if p.name == param), None)
    cases = when["cases"]
    if spec is not None and spec.allowed is not None:
        allowed_s = {str(a) for a in spec.allowed}
        bad = [str(k) for k in cases if str(k) not in allowed_s]
        if bad:
            raise BindError(
                f"{loc} when.cases keys {bad} are not in parameter {param!r} "
                f"allowed {list(spec.allowed)}."
            )
    if force_else_for == param:
        body = when["else"]
    elif force_case is not None and force_case[0] == param:
        label = force_case[1]
        matched = _match_case(cases, label)
        body = cases[matched] if matched is not None else when["else"]
    else:
        bound_s = str(bound.values[param])
        matched = _match_case(cases, bound_s)
        body = cases[matched] if matched is not None else when["else"]
    return _resolve_when_tree(
        body, bound,
        force_else_for=force_else_for,
        force_case=force_case,
        path=path,
    )


def _match_case(cases: Mapping[Any, Any], bound_s: str) -> Any | None:
    for key in cases:
        if str(key) == bound_s:
            return key
    return None


def _is_from_param(node: Any) -> bool:
    return isinstance(node, dict) and set(node.keys()) == {"from_param"}


def _from_param_type(path: tuple[str, ...], measure_raw: Mapping[str, Any] | None) -> str | None:
    if path == ("model",):
        return "string"
    if (
        len(path) == 4
        and path[0] == "measures"
        and path[2] == "trailing"
        and path[3] in TRAILING_UNITS
    ):
        return "int"
    if (
        len(path) == 4
        and path[0] == "measures"
        and path[2] == "offset"
        and path[3] in OFFSET_UNITS
    ):
        return "int"
    if len(path) == 3 and path[0] == "measures" and path[2] == "value":
        kind = None
        if measure_raw is not None:
            kind = measure_raw.get("op") or measure_raw.get("kind")
        if kind != "constant":
            return None
        return "number"
    return None


def _subst_from_params(node: Any, bound: BoundParameters, path: tuple[str, ...] = ()) -> Any:
    return _subst_from_params_ctx(node, bound, path, measure_raw=None)


def _subst_from_params_ctx(
    node: Any,
    bound: BoundParameters,
    path: tuple[str, ...],
    *,
    measure_raw: Mapping[str, Any] | None,
) -> Any:
    if isinstance(node, dict) and "from_param" in node:
        extra = set(node) - {"from_param"}
        if extra:
            raise BindError(
                f"{_path_str(path)} mixes from_param: with other keys {sorted(extra)}; "
                f"a slot is when: or from_param: or a concrete value, not two."
            )
        return _subst_one_ctx(node, bound, path, measure_raw)
    if isinstance(node, dict):
        if len(path) == 1 and path[0] == "measures":
            return {
                k: _subst_from_params_ctx(
                    v, bound, path + (str(k),),
                    measure_raw=v if isinstance(v, dict) else None,
                )
                for k, v in node.items()
            }
        return {
            k: _subst_from_params_ctx(v, bound, path + (str(k),), measure_raw=measure_raw)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            _subst_from_params_ctx(v, bound, path + (str(i),), measure_raw=measure_raw)
            for i, v in enumerate(node)
        ]
    return node


def _subst_one_ctx(
    node: Mapping[str, Any],
    bound: BoundParameters,
    path: tuple[str, ...],
    measure_raw: Mapping[str, Any] | None,
) -> Any:
    loc = _path_str(path)
    name = node.get("from_param")
    if not isinstance(name, str) or not name.strip():
        raise BindError(f"{loc} from_param: must name a declared parameter.")
    if name not in bound.values:
        raise BindError(
            f"{loc} from_param: {name!r} is not a declared parameter."
        )
    want = _from_param_type(path, measure_raw)
    if want is None:
        raise BindError(
            f"{loc} from_param: is not on the allowlist "
            f"(model; measures.*.trailing/offset units; constant measures.*.value)."
        )
    types = {p.name: p.type_name for p in bound.schema}
    got = types.get(name)
    value = bound.values[name]
    if got in {"list", "dict"}:
        raise BindError(
            f"{loc} from_param: {name!r} is type {got}; list/dict cannot fill this field."
        )
    if want == "string":
        if got != "string":
            raise BindError(
                f"{loc} from_param: {name!r} must be type string (model id), got {got}."
            )
        return value
    if want == "int":
        if got != "int" or not isinstance(value, int) or isinstance(value, bool):
            raise BindError(
                f"{loc} from_param: {name!r} must be type int, got {got}={value!r}."
            )
        return value
    if want == "number":
        if got not in {"int", "float"}:
            raise BindError(
                f"{loc} from_param: {name!r} must be type int or float "
                f"(constant value), got {got}."
            )
        return value
    raise BindError(f"{loc} from_param: internal type {want!r}.")
