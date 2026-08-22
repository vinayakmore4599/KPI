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


def _parse_order(key: str, raw: dict[str, Any], kind: str) -> str:
    order = str(raw.get("order") or "desc").strip().lower()
    if order not in {"asc", "desc"}:
        raise BindError(f"measures.{key} op={kind} order must be asc or desc.")
    return order


class _CutBase(OpPlugin):
    """Shared bind/source path for cut-phase add-ons."""

    phase = "cut"
    cut_restricted = True
    extra_keys = frozenset({"order", "partition_by", "group_by"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        raw = dict(common.raw)
        return OutputSpec(
            **{
                **spec.__dict__,
                "rank_order": _parse_order(key, raw, self.name),
                "rank_group_by": support.parse_partition_by(key, self.name, raw),
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
            f"{ctx.spec.key} op={self.name} is assigned after every combo on the cut."
        )


class Ntile(_CutBase):
    name = "ntile"
    extra_keys = _CutBase.extra_keys | frozenset({"tiles"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        tiles = common.raw.get("tiles")
        if not isinstance(tiles, int) or isinstance(tiles, bool) or tiles < 2:
            raise BindError(
                f"measures.{key} op=ntile requires integer tiles: >= 2."
            )
        return OutputSpec(**{**spec.__dict__, "params": {**spec.params, "tiles": tiles}})

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_ntile)


class DenseRank(_CutBase):
    name = "dense_rank"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_dense_rank)


class RowNumber(_CutBase):
    name = "row_number"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_row_number)


class CumulativeShare(_CutBase):
    name = "cumulative_share"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_cumulative_share)


class RunningTotal(_CutBase):
    name = "running_total"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_running_total)


class Contribution(_CutBase):
    name = "contribution"
    extra_keys = _CutBase.extra_keys | frozenset({"vs"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        vs = common.raw.get("vs")
        if not vs:
            raise BindError(f"measures.{key} op=contribution requires `vs:` (the baseline).")
        return OutputSpec(**{**spec.__dict__, "params": {**spec.params, "vs": str(vs)}})

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        super().validate(spec, kpi)
        vs = spec.params.get("vs")
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

    def source_for_cut(self, ctx: EvalCtx) -> Any:
        of_val = _cut_source(ctx)
        vs = ctx.spec.params.get("vs")
        if vs and vs in ctx.catalog:
            vs_val = ctx.evaluate(ctx.catalog[vs])
        else:
            vs_val = ctx.evaluate(OutputSpec(key=str(vs), kind="point", of=str(vs)))
        return of_val, vs_val

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_contribution)


class PercentRank(_CutBase):
    name = "percent_rank"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_percent_rank)


class GapToLeader(_CutBase):
    name = "gap_to_leader"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_gap_to_leader)


class GapToAvg(_CutBase):
    name = "gap_to_avg"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_gap_to_avg)


class ZScore(_CutBase):
    name = "zscore"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_zscore)


class RunningAvg(_CutBase):
    name = "running_avg"

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_running_avg)


class TopN(_CutBase):
    name = "top_n"
    extra_keys = _CutBase.extra_keys | frozenset({"n"})

    def parse(self, key: str, common: CommonMeasureFields) -> OutputSpec:
        spec = super().parse(key, common)
        n = common.raw.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise BindError(f"measures.{key} op=top_n requires integer n: >= 1.")
        return OutputSpec(**{**spec.__dict__, "params": {**spec.params, "n": n}})

    def apply_to_cut(self, cut_rows, spec, cut_dims) -> None:
        _apply_partitioned(cut_rows, spec, cut_dims, write=_write_top_n)


def _tie_key(row: dict[str, Any], spec: OutputSpec) -> tuple[Any, ...]:
    return (spec.key, tuple((dim, row.get(dim)) for dim in spec.rank_group_by), id(row))


def _write_ntile(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ranks = support.sql_rank(values, descending=descending)
    tiles = support.ntile_from_ranks(ranks, int(spec.params["tiles"]))
    for i, tile, source in zip(indexes, tiles, values):
        cut_rows[i][spec.key] = tile
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="ntile",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=tile,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_dense_rank(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ranks = support.dense_rank(values, descending=descending)
    for i, rank, source in zip(indexes, ranks, values):
        cut_rows[i][spec.key] = rank
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="dense_rank",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=rank,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_row_number(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ties = [_tie_key(cut_rows[i], spec) for i in indexes]
    numbers = support.row_numbers(values, descending=descending, tie_keys=ties)
    for i, number, source in zip(indexes, numbers, values):
        cut_rows[i][spec.key] = number
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="row_number",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=number,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _ordered_indexes(values: list[Any], indexes: list[int], *, descending: bool) -> list[int]:
    def sort_key(pos: int):
        number = support.numeric_or_none(values[pos])
        return (number is None, -(number or 0) if descending else (number or 0))

    return sorted(range(len(indexes)), key=sort_key)


def _write_cumulative_share(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    total = sum(v for v in values if v is not None)
    descending = (spec.rank_order or "desc") == "desc"
    running = 0.0
    for pos in _ordered_indexes(values, indexes, descending=descending):
        i = indexes[pos]
        source = values[pos]
        if source is None or total == 0:
            share = None
        else:
            running += source
            share = float(running) * 100.0 / float(total)
        cut_rows[i][spec.key] = share
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="cumulative_share",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=share,
            of=spec.of,
            inputs={spec.of or src: source, "total": total},
        )


def _write_running_total(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    running = 0.0
    for pos in _ordered_indexes(values, indexes, descending=descending):
        i = indexes[pos]
        source = values[pos]
        if source is None:
            total = None
        else:
            running += source
            total = float(running)
        cut_rows[i][spec.key] = total
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="running_total",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=total,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_contribution(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    deltas: list[float | None] = []
    for i in indexes:
        pair = cut_rows[i].get(src)
        if not isinstance(pair, tuple) or len(pair) != 2:
            deltas.append(None)
            continue
        current, baseline = support.numeric_or_none(pair[0]), support.numeric_or_none(pair[1])
        if current is None or baseline is None:
            deltas.append(None)
        else:
            deltas.append(float(current) - float(baseline))
    total = sum(v for v in deltas if v is not None)
    for i, delta in zip(indexes, deltas):
        if delta is None or total == 0:
            share = None
        else:
            share = float(delta) * 100.0 / float(total)
        cut_rows[i][spec.key] = share
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="contribution",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=share,
            of=spec.of,
            inputs={"delta": delta, "total": total},
        )


def _write_percent_rank(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ranks = support.sql_rank(values, descending=descending)
    n = sum(1 for rank in ranks if rank is not None)
    for i, rank, source in zip(indexes, ranks, values):
        if rank is None:
            value = None
        elif n <= 1:
            value = 0.0
        else:
            value = float(rank - 1) * 100.0 / float(n - 1)
        cut_rows[i][spec.key] = value
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="percent_rank",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=value,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_gap_to_leader(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    observed = [v for v in values if v is not None]
    leader = max(observed) if observed else None
    for i, source in zip(indexes, values):
        value = None if source is None or leader is None else float(source) - float(leader)
        cut_rows[i][spec.key] = value
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="gap_to_leader",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=value,
            of=spec.of,
            inputs={spec.of or src: source, "leader": leader},
        )


def _write_gap_to_avg(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    observed = [v for v in values if v is not None]
    mean = sum(observed) / float(len(observed)) if observed else None
    for i, source in zip(indexes, values):
        value = None if source is None or mean is None else float(source) - float(mean)
        cut_rows[i][spec.key] = value
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="gap_to_avg",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=value,
            of=spec.of,
            inputs={spec.of or src: source, "mean": mean},
        )


def _write_zscore(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    observed = [v for v in values if v is not None]
    moments = support.sample_mean_var(observed)
    mean = moments[0] if moments else None
    stdev = (moments[1] ** 0.5) if moments else None
    for i, source in zip(indexes, values):
        if source is None or mean is None or stdev is None:
            value = None
        elif stdev == 0:
            value = 0.0
        else:
            value = (float(source) - float(mean)) / float(stdev)
        cut_rows[i][spec.key] = value
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="zscore",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=value,
            of=spec.of,
            inputs={spec.of or src: source, "mean": mean, "stdev": stdev},
        )


def _write_running_avg(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [support.numeric_or_none(cut_rows[i].get(src)) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    running = 0.0
    count = 0
    for pos in _ordered_indexes(values, indexes, descending=descending):
        i = indexes[pos]
        source = values[pos]
        if source is None:
            avg = None
        else:
            running += source
            count += 1
            avg = float(running) / float(count)
        cut_rows[i][spec.key] = avg
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="running_avg",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=avg,
            of=spec.of,
            inputs={spec.of or src: source},
        )


def _write_top_n(cut_rows, spec: OutputSpec, src: str, indexes: list[int]) -> None:
    values = [cut_rows[i].get(src) for i in indexes]
    descending = (spec.rank_order or "desc") == "desc"
    ranks = support.sql_rank(values, descending=descending)
    n = int(spec.params["n"])
    for i, rank, source in zip(indexes, ranks, values):
        if rank is None:
            flag = None
        else:
            flag = 1.0 if rank <= n else 0.0
        cut_rows[i][spec.key] = flag
        cut_rows[i].pop(src, None)
        log_measure_calc(
            cut=cut_rows[i].get("output_cut") or "",
            key=spec.key,
            op="top_n",
            combo={dim: cut_rows[i].get(dim) for dim in spec.rank_group_by},
            result=flag,
            of=spec.of,
            inputs={spec.of or src: source, "n": n},
        )
