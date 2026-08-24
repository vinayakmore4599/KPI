"""Combo-phase period and compare kinds (lag, lead, index, vs_target, threshold).

Split by family if this file becomes hard to review — never one file per name.
"""

from __future__ import annotations

from typing import Any

from kpi_engine.capabilities.ops import support
from kpi_engine.contracts import NON_ADDITIVE_AGGS, KpiSpec, OutputSpec
from kpi_engine.core.op_protocol import CommonMeasureFields, EvalCtx, OpPlugin
from kpi_engine.exceptions import BindError, CatalogError
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


def _require_offset(spec: OutputSpec) -> None:
    if not _offset_nonzero(spec.offset):
        raise BindError(f"measures.{spec.key} op={spec.kind} requires a non-zero offset.")


def _assert_shift_source(spec: OutputSpec, kpi: KpiSpec) -> None:
    of = spec.of
    known = {b.name for b in kpi.base_measures}
    by_key = {m.key: m for m in kpi.measures}
    if not of:
        raise BindError(
            f"measures.{spec.key} op={spec.kind} requires `of:` naming a base "
            f"or a point/window measure."
        )
    if of in known and of not in by_key:
        helper = next(b for b in kpi.base_measures if b.name == of)
        if helper.agg is None:
            raise BindError(
                f"measures.{spec.key} of={of!r} is a row helper (no agg:)."
            )
        return
    child = by_key.get(of)
    if child is None:
        raise BindError(
            f"measures.{spec.key} of={of!r} is not a base or measure. "
            f"Declared measures: {sorted(by_key)}; base_measures: {sorted(known)}."
        )
    if child.kind not in {"point", "window"}:
        raise BindError(
            f"measures.{spec.key} op={spec.kind} can only shift a base, "
            f"point, or window measure (got op={child.kind})."
        )


def _recompute_at(ctx: EvalCtx, of: str, target) -> Any:
    kpi, series = ctx.kpi, ctx.series
    child = ctx.catalog.get(of)
    bases = {b.name for b in kpi.base_measures}
    if child is None or (of in bases and child.kind not in {"point", "window"}):
        return support.point_value(series, kpi, of, target)
    if child.kind == "point":
        point_at = target
        if child.offset:
            point_at = support.shifted_anchor(target, child.offset, kpi, backward=True)
        base = support.base_measure(kpi, child.of) if child.of else None
        if base is not None and base.agg in NON_ADDITIVE_AGGS:
            return support.agg_detail(
                ctx.detail, kpi, base, ctx.group_dims, ctx.combo, point_at, point_at
            )
        return support.point_value(series, kpi, child.of, point_at)
    if child.kind == "window":
        start, end = support.window_bounds(target, child, kpi)
        base = support.base_measure(kpi, child.of)
        if base.agg in NON_ADDITIVE_AGGS:
            return support.agg_detail(
                ctx.detail, kpi, base, ctx.group_dims, ctx.combo, start, end
            )
        return support.window_value(series, kpi, child, start, end)
    raise CatalogError(f"{ctx.spec.key} cannot recompute {of} op={child.kind} at a shifted anchor.")


def _eval_named(ctx: EvalCtx, name: str) -> Any:
    if name in ctx.catalog:
        return ctx.evaluate(ctx.catalog[name])
    return ctx.evaluate(OutputSpec(key=name, kind="point", of=name))


class _Shift(OpPlugin):
    """lag / lead / index share source rules and lookback."""

    requires_time = True
    backward = True

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        _require_offset(spec)
        _assert_shift_source(spec, kpi)
        if kpi.time is None:
            raise BindError(f"measures.{spec.key} op={self.name} needs a time: block.")

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        return (spec.of,) if spec.of else ()

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        shift = support.offset_lookback(spec.offset, time, anchor)
        child = 0
        if spec.of and spec.of in by_key:
            child = lookback_for(
                by_key[spec.of], by_key, time, anchor=anchor, seen=seen | {spec.key}
            )
        if self.backward:
            return child + shift
        return max(0, child - shift)

    def lookforward(self, spec, by_key, seen, lookforward_for, time=None, anchor=None) -> int:
        if self.backward or spec.offset is None:
            child = 0
            if spec.of and spec.of in by_key:
                child = lookforward_for(
                    by_key[spec.of], by_key, seen=seen | {spec.key}
                )
            return child
        extra = spec.offset.total_months
        child = 0
        if spec.of and spec.of in by_key:
            child = lookforward_for(by_key[spec.of], by_key, seen=seen | {spec.key})
        return child + extra


class Lag(_Shift):
    name = "lag"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=lag needs a time plan.")
        target = support.shifted_anchor(
            ctx.plan.anchor, ctx.spec.offset, ctx.kpi, backward=True
        )
        value = _recompute_at(ctx, ctx.spec.of or "", target)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="lag",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            period=target,
        )
        return value


class Lead(_Shift):
    name = "lead"
    backward = False

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=lead needs a time plan.")
        target = support.shifted_anchor(
            ctx.plan.anchor, ctx.spec.offset, ctx.kpi, backward=False
        )
        value = _recompute_at(ctx, ctx.spec.of or "", target)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="lead",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            period=target,
        )
        return value


class Index(_Shift):
    name = "index"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=index needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target = support.shifted_anchor(
            ctx.plan.anchor, ctx.spec.offset, ctx.kpi, backward=True
        )
        baseline = _recompute_at(ctx, ctx.spec.of or "", target)
        if current is None or baseline in (None, 0):
            value = None
        else:
            value = float(current) * 100.0 / float(baseline)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="index",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            inputs={"current": current, "baseline": baseline},
        )
        return value


class VsTarget(OpPlugin):
    name = "vs_target"
    extra_keys = frozenset({"vs", "as", "value"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        how = str(raw.get("as") or "gap").strip().lower()
        if how not in {"gap", "pct"}:
            raise BindError(f"measures.{key} op=vs_target as: must be gap or pct.")
        vs = raw.get("vs")
        constant = None
        if raw.get("value") is not None:
            try:
                constant = float(raw["value"])
            except (TypeError, ValueError) as exc:
                raise BindError(
                    f"measures.{key} op=vs_target value must be a number."
                ) from exc
        if vs is None and constant is None:
            raise BindError(
                f"measures.{key} op=vs_target requires `vs:` or `value:`."
            )
        return OutputSpec(
            **{
                **spec.__dict__,
                "constant": constant,
                "params": {**spec.params, "vs": str(vs) if vs is not None else None, "as": how},
            }
        )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_measure_or_base_of(spec, kpi)
        vs = spec.params.get("vs")
        if vs:
            by_key = {m.key for m in kpi.measures}
            known = {b.name for b in kpi.base_measures}
            if vs not in by_key and vs not in known:
                raise BindError(
                    f"measures.{spec.key} vs={vs!r} is not a measure or base measure."
                )

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        names = [n for n in (spec.of, spec.params.get("vs")) if n]
        return tuple(names)

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        deeper = seen | {spec.key}
        return max(
            (
                lookback_for(by_key[n], by_key, time, anchor=anchor, seen=deeper)
                for n in self.dependencies(spec)
                if n in by_key
            ),
            default=0,
        )

    def evaluate(self, ctx: EvalCtx) -> Any:
        actual = _eval_named(ctx, ctx.spec.of or "")
        vs = ctx.spec.params.get("vs")
        target = ctx.spec.constant if vs is None else _eval_named(ctx, str(vs))
        how = ctx.spec.params.get("as") or "gap"
        if actual is None or target is None:
            value = None
        elif how == "pct":
            value = None if target == 0 else (float(actual) - float(target)) * 100.0 / float(target)
        else:
            value = float(actual) - float(target)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="vs_target",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            inputs={"actual": actual, "target": target},
        )
        return value


class Threshold(OpPlugin):
    name = "threshold"
    extra_keys = frozenset({"cmp", "value", "vs"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        cmp = str(raw.get("cmp") or "gte").strip().lower()
        if cmp not in {"gt", "gte", "lt", "lte", "eq"}:
            raise BindError(
                f"measures.{key} op=threshold cmp must be gt, gte, lt, lte, or eq."
            )
        vs = raw.get("vs")
        constant = None
        if raw.get("value") is not None:
            try:
                constant = float(raw["value"])
            except (TypeError, ValueError) as exc:
                raise BindError(
                    f"measures.{key} op=threshold value must be a number."
                ) from exc
        if vs is None and constant is None:
            raise BindError(f"measures.{key} op=threshold requires `value:` or `vs:`.")
        return OutputSpec(
            **{
                **spec.__dict__,
                "constant": constant,
                "params": {**spec.params, "cmp": cmp, "vs": str(vs) if vs is not None else None},
            }
        )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_measure_or_base_of(spec, kpi)

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        names = [n for n in (spec.of, spec.params.get("vs")) if n]
        return tuple(names)

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        deeper = seen | {spec.key}
        return max(
            (
                lookback_for(by_key[n], by_key, time, anchor=anchor, seen=deeper)
                for n in self.dependencies(spec)
                if n in by_key
            ),
            default=0,
        )

    def evaluate(self, ctx: EvalCtx) -> Any:
        actual = _eval_named(ctx, ctx.spec.of or "")
        vs = ctx.spec.params.get("vs")
        bar = ctx.spec.constant if vs is None else _eval_named(ctx, str(vs))
        if actual is None or bar is None:
            value = None
        else:
            cmp = ctx.spec.params.get("cmp") or "gte"
            left, right = float(actual), float(bar)
            hit = {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
                "eq": left == right,
            }[cmp]
            value = 1.0 if hit else 0.0
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="threshold",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            inputs={"actual": actual, "bar": bar},
        )
        return value


class Diff(_Shift):
    name = "diff"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=diff needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target = support.shifted_anchor(
            ctx.plan.anchor, ctx.spec.offset, ctx.kpi, backward=True
        )
        baseline = _recompute_at(ctx, ctx.spec.of or "", target)
        if current is None or baseline is None:
            value = None
        else:
            value = float(current) - float(baseline)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="diff",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            inputs={"current": current, "baseline": baseline},
        )
        return value


class PctChange(_Shift):
    name = "pct_change"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=pct_change needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target = support.shifted_anchor(
            ctx.plan.anchor, ctx.spec.offset, ctx.kpi, backward=True
        )
        baseline = _recompute_at(ctx, ctx.spec.of or "", target)
        if current is None or baseline in (None, 0):
            value = None
        else:
            value = float(current - baseline) / float(baseline)
        log_measure_calc(
            cut=ctx.cut,
            key=ctx.spec.key,
            op="pct_change",
            combo=_combo(ctx),
            result=value,
            of=ctx.spec.of,
            inputs={"current": current, "baseline": baseline},
        )
        return value
