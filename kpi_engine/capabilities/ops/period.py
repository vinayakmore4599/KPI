"""Combo-phase period and compare kinds (lag, lead, index, vs_target, threshold).

Split by family if this file becomes hard to review — never one file per name.
"""

from __future__ import annotations

from typing import Any

from kpi_engine.capabilities.ops import support
from kpi_engine.contracts import KpiSpec, OutputSpec
from kpi_engine.pipeline.op_protocol import CommonMeasureFields, EvalCtx, OpPlugin, offset_is_nonzero
from kpi_engine.pipeline.op_registry import get_op
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
    return offset_is_nonzero(offset)


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
            f"or a shiftable measure."
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
    leaf = _unshiftable_leaf(child, by_key, seen=frozenset())
    if leaf is None:
        return
    leaf_spec, label = leaf
    hint = ""
    if leaf_spec.kind in {
        "rank",
        "dense_rank",
        "percent_rank",
        "ntile",
        "row_number",
        "top_n",
        "percent_of_total",
    }:
        hint = " Rank a lagged measure (rank of lag), do not lag a rank."
    if leaf_spec.kind == "trend":
        hint = " Put offset: on the trend measure itself."
    raise BindError(
        f"measures.{spec.key} op={spec.kind} cannot shift {of!r}: "
        f"{label} is not shiftable.{hint}"
    )


def _unshiftable_leaf(
    spec: OutputSpec,
    by_key: dict[str, OutputSpec],
    seen: frozenset[str],
) -> tuple[OutputSpec, str] | None:
    """First non-shiftable descendant, named as the leaf for BindError."""
    if spec.key in seen:
        return None
    plugin = get_op(spec.kind)
    if not plugin.shiftable:
        return spec, f"{spec.key} op={spec.kind}"
    for name in plugin.dependencies(spec):
        child = by_key.get(name)
        if child is None:
            continue
        hit = _unshiftable_leaf(child, by_key, seen | {spec.key})
        if hit is not None:
            return hit
    return None


def _eval_named(ctx: EvalCtx, name: str, *, anchor=None, selection=None) -> Any:
    spec = ctx.catalog.get(name)
    if spec is None:
        spec = OutputSpec(key=name, kind="point", of=name)
    kwargs: dict[str, Any] = {}
    if anchor is not None:
        kwargs["anchor"] = anchor
    if selection is not None:
        kwargs["selection"] = selection
    if kwargs:
        return ctx.evaluate(spec, **kwargs)
    return ctx.evaluate(spec)


def _shifted_selection(ctx: EvalCtx, *, backward: bool) -> tuple:
    from kpi_engine.pipeline.period_select import negate_offset, shift_selection

    periods = support.effective_selection(ctx)
    if not periods or ctx.spec.offset is None or ctx.kpi.time is None:
        return periods
    offset = negate_offset(ctx.spec.offset) if backward else ctx.spec.offset
    return shift_selection(periods, offset, ctx.kpi.time)


class _Shift(OpPlugin):
    """lag / lead / index share source rules and lookback."""

    requires_time = True
    backward = True
    shiftable = True

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
        target_sel = _shifted_selection(ctx, backward=True)
        if not target_sel:
            value = None
            target = None
        else:
            value = _eval_named(ctx, ctx.spec.of or "", selection=target_sel)
            target = target_sel[-1]
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

    def periods(self, spec, kpi, plan):
        return support.shift_period_meta(spec, kpi, plan, backward=True)


class Lead(_Shift):
    name = "lead"
    backward = False

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=lead needs a time plan.")
        target_sel = _shifted_selection(ctx, backward=False)
        if not target_sel:
            value = None
            target = None
        else:
            value = _eval_named(ctx, ctx.spec.of or "", selection=target_sel)
            target = target_sel[-1]
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

    def periods(self, spec, kpi, plan):
        return support.shift_period_meta(spec, kpi, plan, backward=False)


class Index(_Shift):
    name = "index"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=index needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target_sel = _shifted_selection(ctx, backward=True)
        baseline = (
            _eval_named(ctx, ctx.spec.of or "", selection=target_sel)
            if target_sel
            else None
        )
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

    def periods(self, spec, kpi, plan):
        return support.compare_period_meta(spec, kpi, plan)


class VsTarget(OpPlugin):
    name = "vs_target"
    extra_keys = frozenset({"vs", "as", "value"})
    shiftable = True

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

    def periods(self, spec, kpi, plan):
        return support.current_period_meta(kpi, plan)


class Threshold(OpPlugin):
    name = "threshold"
    extra_keys = frozenset({"cmp", "value", "vs"})
    shiftable = True

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

    def periods(self, spec, kpi, plan):
        return support.current_period_meta(kpi, plan)


class Diff(_Shift):
    name = "diff"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=diff needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target_sel = _shifted_selection(ctx, backward=True)
        baseline = (
            _eval_named(ctx, ctx.spec.of or "", selection=target_sel)
            if target_sel
            else None
        )
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

    def periods(self, spec, kpi, plan):
        return support.compare_period_meta(spec, kpi, plan)


class PctChange(_Shift):
    name = "pct_change"
    backward = True

    def evaluate(self, ctx: EvalCtx) -> Any:
        if ctx.plan is None:
            raise CatalogError(f"{ctx.spec.key} op=pct_change needs a time plan.")
        current = _eval_named(ctx, ctx.spec.of or "")
        target_sel = _shifted_selection(ctx, backward=True)
        baseline = (
            _eval_named(ctx, ctx.spec.of or "", selection=target_sel)
            if target_sel
            else None
        )
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

    def periods(self, spec, kpi, plan):
        return support.compare_period_meta(spec, kpi, plan)


class Compare(OpPlugin):
    """Bind-time sugar. Binder expands to pct_change or diff; never evaluated."""

    name = "compare"
    extra_keys = frozenset({"mode", "versus"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        return OutputSpec(
            **{
                **spec.__dict__,
                "params": {
                    **spec.params,
                    "mode": raw.get("mode"),
                    "versus": raw.get("versus"),
                },
            }
        )
