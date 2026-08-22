"""Cut-phase measure kinds (rank, percent_of_total). Add ntile-style ops here."""

from __future__ import annotations

from typing import Any

from kpi_engine.capabilities.ops import support
from kpi_engine.contracts import KpiSpec, OutputSpec
from kpi_engine.core.op_protocol import CommonMeasureFields, EvalCtx, OpPlugin
from kpi_engine.exceptions import BindError, CatalogError
from kpi_engine.identifiers import norm_name
from kpi_engine.runlog import log_measure_calc


class Rank(OpPlugin):
    name = "rank"
    phase = "cut"
    cut_restricted = True
    extra_keys = frozenset({"order", "partition_by", "group_by"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        order = str(raw.get("order") or "desc").strip().lower()
        if order not in {"asc", "desc"}:
            raise BindError(f"measures.{key} op=rank order must be asc or desc.")
        return OutputSpec(
            **{
                **spec.__dict__,
                "rank_order": order,
                "rank_group_by": support.parse_partition_by(key, "rank", raw),
            }
        )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_measure_or_base_of(spec, kpi)
        support.assert_partition_keys(spec, kpi)

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        return (spec.of,) if spec.of else ()

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        if spec.of and spec.of in by_key:
            return lookback_for(
                by_key[spec.of], by_key, time, anchor=anchor, seen=seen | {spec.key}
            )
        return 0

    def source_for_cut(self, ctx: EvalCtx) -> Any:
        return _cut_source(ctx)

    def evaluate(self, ctx: EvalCtx) -> Any:
        raise CatalogError(f"{ctx.spec.key} op=rank is assigned after every combo on the cut.")

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(
            cut_rows,
            spec,
            cut_dims,
            write=_write_rank,
        )


class PercentOfTotal(OpPlugin):
    name = "percent_of_total"
    phase = "cut"
    cut_restricted = True
    extra_keys = frozenset({"partition_by", "group_by"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        return OutputSpec(
            **{
                **spec.__dict__,
                "rank_group_by": support.parse_partition_by(key, "percent_of_total", raw),
            }
        )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        support.require_measure_or_base_of(spec, kpi)
        support.assert_partition_keys(spec, kpi)

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        return (spec.of,) if spec.of else ()

    def lookback(self, spec, by_key, time, anchor, seen, lookback_for) -> int:
        if spec.of and spec.of in by_key:
            return lookback_for(
                by_key[spec.of], by_key, time, anchor=anchor, seen=seen | {spec.key}
            )
        return 0

    def source_for_cut(self, ctx: EvalCtx) -> Any:
        return _cut_source(ctx)

    def evaluate(self, ctx: EvalCtx) -> Any:
        raise CatalogError(
            f"{ctx.spec.key} op=percent_of_total is assigned after every combo on the cut."
        )

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_share)


def _cut_source(ctx: EvalCtx) -> Any:
    of = ctx.spec.of
    if of and of in ctx.catalog:
        return ctx.evaluate(ctx.catalog[of])
    return ctx.evaluate(OutputSpec(key=of or ctx.spec.key, kind="point", of=of))


def _apply_partitioned(cut_rows, spec: OutputSpec, cut_dims: list[str], *, write) -> None:
    src = f"__cut_src_{spec.key}"
    partition_keys = spec.rank_group_by
    if {norm_name(n) for n in partition_keys} == {norm_name(n) for n in cut_dims}:
        partition_keys = ()
    partitions: dict[tuple[Any, ...], list[int]] = {}
    for i, row in enumerate(cut_rows):
        part = support.rank_partition(row, partition_keys)
        partitions.setdefault(part, []).append(i)
    for indexes in partitions.values():
        write(cut_rows, spec, src, indexes)


def _write_rank(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ranks = support.sql_rank(values, descending=descending)
    for i, rank, source in zip(indexes, ranks, values):
        cut_rows[i][spec.key] = rank
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="rank",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=rank,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_share(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    total = sum(v for v in values if v is not None)
    for i, source in zip(indexes, values):
        if source is None or total == 0:
            share = None
        else:
            share = float(source) * 100.0 / float(total)
        cut_rows[i][spec.key] = share
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="percent_of_total",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=share,
            of=spec.of,
            inputs={spec.of or src: source, "total": total},
        )
