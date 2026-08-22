"""Combo-phase measure kinds (point, window, trend, arithmetic, fn, expr, constant, dimension, hook).

Split this file by family when it is hard to review — never one file per name.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from kpi_engine.capabilities.ops import support
from kpi_engine.contracts import (
    NON_ADDITIVE_AGGS,
    KpiSpec,
    OutputSpec,
    TimeSpec,
)
from kpi_engine.core.op_protocol import CommonMeasureFields, EvalCtx, OpPlugin
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.identifiers import expression_columns, parse_expression
from kpi_engine.runlog import log_measure_calc


def _combo(ctx: EvalCtx) -> dict[str, Any]:
    if ctx.combo is None or not ctx.group_dims:
        return {}
    out: dict[str, Any] = {}
    for dim in ctx.group_dims:
        out[dim] = ctx.combo[dim] if dim in ctx.combo.index else None
    return out


def _offset_nonzero(offset) -> bool:
    if offset is None:
        return False
    return bool(offset.months or offset.years or offset.days or offset.quarters)


class Point(OpPlugin):
    name = "point"

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_base_of(spec, kpi)
        if kpi.time is None and _offset_nonzero(spec.offset):
            raise BindError(
                f"measures.{spec.key} (point offset) needs a time: block."
            )

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        if not spec.offset:
            return 0
        if time is None or (
            time.grain == "month" and spec.offset.days == 0 and spec.offset.quarters == 0
        ):
            return spec.offset.total_months
        from kpi_engine.dates import apply_offset, periods_between, truncate_period
        from datetime import date as date_cls

        dummy = date_cls(2021, 6, 15)
        dummy_t = truncate_period(dummy, time)
        target = truncate_period(apply_offset(dummy, spec.offset), time)
        return periods_between(target, dummy_t, time)

    def evaluate(self, ctx: EvalCtx) -> Any:
        spec, kpi, plan = ctx.spec, ctx.kpi, ctx.plan
        combo = _combo(ctx)
        if kpi.time is None or plan is None:
            value = support.point_value(ctx.series, kpi, spec.of, target=None)
            support.log_base_calc(
                spec, ctx.cut, combo, value, ctx.series, kpi, start=None, end=None
            )
            return value
        target = support.truncate_period_safe(plan.anchor, kpi)
        if spec.offset:
            from kpi_engine.dates import apply_offset, truncate_period

            lookback = replace(
                spec.offset,
                days=-spec.offset.days,
                months=-spec.offset.months,
                years=-spec.offset.years,
                quarters=-spec.offset.quarters,
            )
            target = truncate_period(apply_offset(plan.anchor, lookback), kpi.time)
        base = support.base_measure(kpi, spec.of) if spec.of else None
        if base is not None and base.agg in NON_ADDITIVE_AGGS:
            value = support.agg_detail(
                ctx.detail, kpi, base, ctx.group_dims, ctx.combo, target, target
            )
        else:
            value = support.point_value(ctx.series, kpi, spec.of, target)
        support.log_base_calc(
            spec, ctx.cut, combo, value, ctx.series, kpi, start=target, end=target
        )
        return value


class Window(OpPlugin):
    name = "window"
    requires_time = True

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_base_of(spec, kpi)
        if kpi.time is None:
            raise BindError(f"measures.{spec.key} (window) needs a time: block.")

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        return _window_lookback(spec, time, anchor)

    def lookforward(self, spec, by_key, seen, lookforward_for) -> int:
        if (spec.window_range or "trailing") != "leading":
            return 0
        n = spec.trailing_months or 1
        return max(n - 1, 0) if spec.inclusive else n

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.kpi.time is None or ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} is a window measure; this KPI has no time column.")
        start, end = support.window_bounds(ctx.plan.anchor, ctx.spec, ctx.kpi)
        base = support.base_measure(ctx.kpi, ctx.spec.of)
        if base.agg in NON_ADDITIVE_AGGS:
            value = support.agg_detail(
                ctx.detail, ctx.kpi, base, ctx.group_dims, ctx.combo, start, end
            )
        else:
            value = support.window_value(ctx.series, ctx.kpi, ctx.spec, start, end)
        support.log_base_calc(
            ctx.spec,
            ctx.cut,
            _combo(ctx),
            value,
            ctx.series,
            ctx.kpi,
            start=start,
            end=end,
        )
        return value


class Trend(OpPlugin):
    name = "trend"
    requires_time = True
    cut_restricted = True
    emits_trend = True

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_base_of(spec, kpi)
        if kpi.time is None:
            raise BindError(f"measures.{spec.key} (trend) needs a time: block.")

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        return _window_lookback(spec, time, anchor)

    def lookforward(self, spec, by_key, seen, lookforward_for) -> int:
        if (spec.window_range or "trailing") != "leading":
            return 0
        n = spec.trailing_months or 1
        return max(n - 1, 0) if spec.inclusive else n

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.kpi.time is None or ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} is a trend measure; this KPI has no time column.")
        return support.trend_values(
            ctx.series,
            ctx.kpi,
            ctx.spec,
            ctx.plan,
            detail=ctx.detail,
            combo=ctx.combo,
            group_dims=ctx.group_dims,
        )


class Arithmetic(OpPlugin):
    name = "arithmetic"
    extra_keys = frozenset({"fn", "left", "right"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        from kpi_engine.core.fn_apply import MEASURE_FNS, measure_fn_error

        raw = common.raw
        fn = str(raw.get("fn") or "divide")
        if fn not in MEASURE_FNS:
            raise BindError(
                f"measures.{key} names unknown fn {fn!r}. Registered: {sorted(MEASURE_FNS)}."
            )
        operand_count = len(common.operands) or len(
            [n for n in (raw.get("left"), raw.get("right")) if n]
        )
        problem = measure_fn_error(fn, operand_count or 2)
        if problem:
            raise BindError(f"measures.{key} fn {problem}")
        return OutputSpec(
            **{**spec.__dict__, "fn": raw.get("fn"), "left": raw.get("left"), "right": raw.get("right")}
        )

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        if spec.operands:
            return spec.operands
        return tuple(n for n in (spec.left, spec.right) if n)

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        return _max_dep_lookback(spec, by_key, time, anchor, seen, lookback_for)

    def lookforward(self, spec, by_key, seen, lookforward_for) -> int:
        return _max_dep_lookforward(spec, by_key, seen, lookforward_for)

    def evaluate(self, ctx: EvalCtx) -> Any:
        return _compose(ctx, kind="arithmetic")


class Fn(OpPlugin):
    name = "fn"
    extra_keys = frozenset({"fn", "inputs"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        from kpi_engine.core.fn_apply import MEASURE_FNS, measure_fn_error

        raw = common.raw
        if not raw.get("inputs"):
            raise BindError(
                f"measures.{key} op=fn requires `inputs:` listing the measures to feed it."
            )
        if not raw.get("fn"):
            raise BindError(
                f"measures.{key} op=fn requires `fn:` (a registered measure function)."
            )
        inputs, input_params = support.parse_fn_inputs(key, raw.get("inputs"))
        fn_name = str(raw["fn"])
        if fn_name not in MEASURE_FNS:
            raise BindError(
                f"measures.{key} names unknown fn {fn_name!r}. Registered: {sorted(MEASURE_FNS)}."
            )
        problem = measure_fn_error(fn_name, len(inputs), input_params)
        if problem:
            raise BindError(f"measures.{key} fn {problem}")
        return OutputSpec(
            **{**spec.__dict__, "fn": raw.get("fn"), "inputs": inputs, "input_params": input_params}
        )

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        return spec.inputs

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        return _max_dep_lookback(spec, by_key, time, anchor, seen, lookback_for)

    def lookforward(self, spec, by_key, seen, lookforward_for) -> int:
        return _max_dep_lookforward(spec, by_key, seen, lookforward_for)

    def evaluate(self, ctx: EvalCtx) -> Any:
        return _compose(ctx, kind="fn")


class Expr(OpPlugin):
    name = "expr"
    extra_keys = frozenset({"expr", "inputs"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = common.raw
        expr_raw = raw.get("expr")
        if not expr_raw or not str(expr_raw).strip():
            raise BindError(f"measures.{key} op=expr requires `expr:` with a formula.")
        expr = str(expr_raw).strip()
        node = parse_expression(expr, what=f"measures.{key}.expr")
        names = expression_columns(node)
        if not names:
            raise BindError(f"measures.{key} expr must name at least one other measure.")
        if raw.get("inputs"):
            inputs, input_params = support.parse_fn_inputs(key, raw.get("inputs"))
            bindable = input_params or inputs
            unknown = [n for n in names if n not in bindable]
            if unknown:
                raise BindError(
                    f"measures.{key} expr names {unknown[0]!r}, which is not in inputs: {list(bindable)}."
                )
        else:
            inputs, input_params = names, ()
        return OutputSpec(
            **{
                **spec.__dict__,
                "expr": expr,
                "inputs": inputs,
                "input_params": input_params,
            }
        )

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        return spec.inputs

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        return _max_dep_lookback(spec, by_key, time, anchor, seen, lookback_for)

    def lookforward(self, spec, by_key, seen, lookforward_for) -> int:
        return _max_dep_lookforward(spec, by_key, seen, lookforward_for)

    def evaluate(self, ctx: EvalCtx) -> Any:
        return _compose(ctx, kind="expr")


class Constant(OpPlugin):
    name = "constant"
    extra_keys = frozenset({"value"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = common.raw
        if raw.get("value") is None:
            raise BindError(f"measures.{key} op=constant requires `value:`.")
        try:
            constant = float(raw["value"])
        except (TypeError, ValueError) as exc:
            raise BindError(
                f"measures.{key} op=constant value must be a number (got {raw.get('value')!r})."
            ) from exc
        return OutputSpec(**{**spec.__dict__, "constant": constant})

    def evaluate(self, ctx: EvalCtx) -> Any:
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="constant",
            combo=_combo(ctx),
            result=ctx.spec.constant,
        )
        return ctx.spec.constant


class Dimension(OpPlugin):
    name = "dimension"
    echo_dimension = True

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        if spec.key not in kpi.dimensions:
            raise BindError(
                f"measures.{spec.key} is a dimension measure but {spec.key!r} is not in "
                f"dimensions: {sorted(kpi.dimensions)}."
            )

    def evaluate(self, ctx: EvalCtx) -> Any:
        return None


class Hook(OpPlugin):
    name = "hook"
    extra_keys = frozenset({"hook", "fn", "value"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = common.raw
        hook = raw.get("hook") or raw.get("fn")
        if not hook:
            raise BindError(f"measures.{key} op=hook requires `hook:` (an allowlisted name).")
        from kpi_engine.extensions.hooks import REGISTRY

        if str(hook) not in REGISTRY:
            raise BindError(
                f"measures.{key} names unknown hook {hook!r}. "
                "Register it in registries/hooks.yaml."
            )
        constant = spec.constant
        if raw.get("value") is not None:
            try:
                constant = float(raw["value"])
            except (TypeError, ValueError) as exc:
                raise BindError(
                    f"measures.{key} op=hook value must be a number."
                ) from exc
        return OutputSpec(
            **{**spec.__dict__, "hook": str(hook), "fn": raw.get("fn"), "constant": constant}
        )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        if spec.of:
            support.require_base_of(spec, kpi)
        if kpi.time is None and (_offset_nonzero(spec.offset) or spec.trailing_months):
            raise BindError(f"measures.{spec.key} (hook lookback) needs a time: block.")
        if spec.hook in {"hit_rate", "streak"} and spec.constant is None:
            raise BindError(
                f"measures.{spec.key} hook={spec.hook} requires `value:` (the bar)."
            )

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        if spec.offset:
            if time is None or time.grain == "month":
                return spec.offset.total_months
            from kpi_engine.dates import apply_offset, periods_between, truncate_period
            from datetime import date as date_cls

            dummy = date_cls(2021, 6, 15)
            dummy_t = truncate_period(dummy, time)
            target = truncate_period(apply_offset(dummy, spec.offset), time)
            return periods_between(target, dummy_t, time)
        if spec.trailing_months:
            n = spec.trailing_months
            return max(n - 1, 0) if spec.inclusive else n
        return 0

    def evaluate(self, ctx: EvalCtx) -> Any:
        from kpi_engine.extensions.hooks import run

        name = ctx.spec.hook or ctx.spec.fn
        if not name:
            raise CatalogError(f"measures.{ctx.spec.key} op=hook requires `hook:`.")
        value = run(name, ctx.series, kpi=ctx.kpi, plan=ctx.plan, spec=ctx.spec)
        log_measure_calc(
            cut=ctx.cut, key=ctx.spec.key, op="hook", combo=_combo(ctx), result=value, hook=name
        )
        return value


def _window_lookback(spec: OutputSpec, time: TimeSpec | None, anchor) -> int:
    kind = spec.window_range or "trailing"
    if kind == "leading":
        return 0
    if kind == "cumulative":
        if time is None:
            return 0
        from datetime import date as date_cls
        from kpi_engine.dates import periods_between, truncate_period, year_start

        ref = truncate_period(anchor or date_cls(2021, 12, 15), time)
        start = year_start(ref, time)
        return periods_between(start, ref, time)
    n = spec.trailing_months or 1
    if spec.trailing_unit == "day" and time is not None and time.grain != "day":
        from datetime import date as date_cls
        from kpi_engine.dates import add_days, periods_between, truncate_period

        ref = truncate_period(anchor or date_cls(2021, 6, 15), time)
        start = add_days(ref, -(n - 1 if spec.inclusive else n))
        return periods_between(truncate_period(start, time), ref, time)
    if spec.inclusive:
        return max(n - 1, 0)
    return n


def _dep_keys(spec: OutputSpec) -> list[str]:
    if spec.kind in {"fn", "expr"}:
        return list(spec.inputs)
    if spec.operands:
        return list(spec.operands)
    return [n for n in (spec.left, spec.right) if n]


def _max_dep_lookback(spec, by_key, time, anchor, seen, lookback_for) -> int:
    deeper = seen | {spec.key}
    return max(
        (
            lookback_for(by_key[n], by_key, time, anchor=anchor, seen=deeper)
            for n in _dep_keys(spec)
            if n in by_key
        ),
        default=0,
    )


def _max_dep_lookforward(spec, by_key, seen, lookforward_for) -> int:
    deeper = seen | {spec.key}
    return max(
        (
            lookforward_for(by_key[n], by_key, seen=deeper)
            for n in _dep_keys(spec)
            if n in by_key
        ),
        default=0,
    )


def _compose(ctx: EvalCtx, *, kind: str) -> Any:
    from kpi_engine.core.fn_apply import call_measure_fn, eval_expr_scalar

    spec = ctx.spec
    if kind == "fn":
        names = list(spec.inputs)
    elif kind == "expr":
        names = list(spec.inputs)
    else:
        names = list(spec.operands) or [spec.left or "", spec.right or ""]
    missing = [n for n in names if n not in ctx.catalog]
    if missing:
        raise CatalogError(f"{spec.key} references measures that do not exist: {missing}.")
    values = [ctx.evaluate(ctx.catalog[n]) for n in names]
    combo = _combo(ctx)
    if kind == "expr":
        keys = list(spec.input_params) if spec.input_params else names
        result = eval_expr_scalar(
            parse_expression(spec.expr or "", what=f"measures.{spec.key}.expr"),
            dict(zip(keys, values)),
        )
        log_measure_calc(
            cut=ctx.cut,
            key=spec.key,
            op="expr",
            combo=combo,
            result=result,
            expr=spec.expr,
            inputs=dict(zip(keys, values)),
        )
        return result
    result = call_measure_fn(
        spec.fn or ("divide" if kind == "arithmetic" else ""),
        values,
        spec.input_params if kind == "fn" else (),
    )
    keys = list(spec.input_params) if kind == "fn" and spec.input_params else names
    log_measure_calc(
        cut=ctx.cut,
        key=spec.key,
        op=kind,
        combo=combo,
        result=result,
        fn=spec.fn or ("divide" if kind == "arithmetic" else None),
        inputs=dict(zip(keys, values)),
    )
    return result
