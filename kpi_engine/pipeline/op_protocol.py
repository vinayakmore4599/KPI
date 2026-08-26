"""Frozen plugin façade: OpPlugin, EvalCtx, CommonMeasureFields.

Capabilities must import this module instead of binder / calc_engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

from kpi_engine.contracts import (
    KpiSpec,
    Offset,
    OutputSpec,
    TimePlan,
    TimeSpec,
)


@runtime_checkable
class EvaluateFn(Protocol):
    """Child evaluator. Keyword-only `anchor` overrides the inherited period."""

    def __call__(
        self,
        spec: OutputSpec,
        *,
        anchor: date | None = None,
        selection: tuple[date, ...] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class EvalCtx:
    """Everything a combo-phase plugin may read while evaluating one group."""

    spec: OutputSpec
    series: pd.DataFrame
    kpi: KpiSpec
    plan: TimePlan | None
    catalog: dict[str, OutputSpec]
    detail: pd.DataFrame | None
    combo: pd.Series | None
    group_dims: list[str]
    memo: dict[str, Any]
    cut: str
    evaluate: EvaluateFn
    anchor: date | None = None
    selection: tuple[date, ...] | None = None


@dataclass(frozen=True)
class CommonMeasureFields:
    """Shared YAML fields binder extracts before plugin.parse."""

    of: str | None
    operands: tuple[str, ...]
    offset: Offset | None
    trailing_months: int | None
    inclusive: bool
    cuts: tuple[str, ...] | None
    window_range: str | None
    trailing_from: str | None = None
    trailing_unit: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    parameter_names: frozenset[str] = field(default_factory=frozenset)
    parameter_types: Mapping[str, str] = field(default_factory=dict)


def offset_is_nonzero(offset: Offset | None) -> bool:
    """True when a calendar or grain-period offset actually shifts the anchor."""
    if offset is None:
        return False
    return bool(
        offset.months
        or offset.years
        or offset.days
        or offset.quarters
        or offset.weeks
        or offset.periods
    )


class OpPlugin:
    """One measure kind. Register via registries/ops.yaml, not by editing core."""

    name: str = ""
    aliases: tuple[str, ...] = ()
    phase: Literal["combo", "cut"] = "combo"
    cut_restricted: bool = False
    requires_time: bool = False
    emits_trend: bool = False
    echo_dimension: bool = False
    extra_keys: frozenset[str] = frozenset()
    shiftable: bool = False

    def needs_time(self, spec: OutputSpec) -> bool:
        """True when this measure cannot run on a snapshot KPI (no time: block)."""
        return (
            self.requires_time
            or offset_is_nonzero(spec.offset)
            or bool(spec.trailing_months)
            or bool(spec.trailing_from)
        )

    def parse(
        self,
        key: str,
        common: CommonMeasureFields,
        extra_allowed: frozenset[str] | tuple[str, ...] = (),
    ) -> OutputSpec:
        """Build OutputSpec from shared fields plus this kind's extras."""
        self.assert_known_keys(key, common, extra_allowed=extra_allowed)
        return OutputSpec(
            key=str(key),
            kind=self.name,
            of=common.of,
            offset=common.offset,
            trailing_months=common.trailing_months,
            trailing_from=common.trailing_from,
            inclusive=common.inclusive,
            cuts=common.cuts,
            window_range=common.window_range,  # type: ignore[arg-type]
            trailing_unit=common.trailing_unit,  # type: ignore[arg-type]
            operands=common.operands,
        )

    def assert_known_keys(
        self,
        key: str,
        common: CommonMeasureFields,
        extra_allowed: frozenset[str] | tuple[str, ...] = (),
    ) -> None:
        """Reject YAML keys this kind does not understand."""
        allowed = self.extra_keys | frozenset(extra_allowed) | {
            "kind",
            "op",
            "of",
            "offset",
            "trailing",
            "inclusive",
            "cuts",
            "range",
            "where",
            "ignore_filters",
        }
        unknown = [name for name in common.raw if name not in allowed]
        if unknown:
            from kpi_engine.exceptions import BindError

            name = unknown[0]
            if name in {"fn", "inputs", "expr"}:
                raise BindError(
                    f"measures.{key} op={self.name} ignores `{name}:`. "
                    "Use op: fn with inputs:, or op: arithmetic, or op: hook."
                    if name == "fn"
                    else f"measures.{key} op={self.name} ignores `{name}:`. "
                    "Use op: fn or op: expr."
                    if name == "inputs"
                    else f"measures.{key} op={self.name} ignores `expr:`. Use op: expr."
                )
            raise BindError(
                f"measures.{key} op={self.name} does not accept {name!r}."
            )

    def validate(self, spec: OutputSpec, kpi: KpiSpec) -> None:
        """Kind-specific bind checks. Default: require `of` when the plugin says so."""

    def dependencies(self, spec: OutputSpec) -> tuple[str, ...]:
        """Measure keys this measure consumes."""
        return ()

    def lookback(
        self,
        spec: OutputSpec,
        by_key: dict[str, OutputSpec],
        time: TimeSpec | None,
        anchor: Any,
        seen: frozenset[str],
        lookback_for: Callable[..., int],
    ) -> int:
        """Grain periods before the anchor this measure needs."""
        return 0

    def lookforward(
        self,
        spec: OutputSpec,
        by_key: dict[str, OutputSpec],
        seen: frozenset[str],
        lookforward_for: Callable[..., int],
        time: TimeSpec | None = None,
        anchor: Any = None,
    ) -> int:
        """Grain periods after the anchor a leading or full-period window needs."""
        return 0

    def evaluate(self, ctx: EvalCtx) -> Any:
        """Combo-phase value for one dimension combo."""
        raise NotImplementedError(f"{self.name} does not implement evaluate.")

    def source_for_cut(self, ctx: EvalCtx) -> Any:
        """Stash value used by apply_to_cut after every combo."""
        raise NotImplementedError(f"{self.name} does not implement source_for_cut.")

    def apply_to_cut(
        self,
        cut_rows: list[dict[str, Any]],
        spec: OutputSpec,
        cut_dims: list[str],
        *,
        totals: dict | None = None,
    ) -> None:
        """Write cut-phase results onto every row of one cut."""
        raise NotImplementedError(f"{self.name} does not implement apply_to_cut.")

    def periods(
        self,
        spec: OutputSpec,
        kpi: KpiSpec,
        plan: TimePlan | None,
    ) -> dict[str, Any] | None:
        """Envelope period metadata for wrapping a scalar or trend after calc.

        Return ``{"period": iso}``, ``{"period_start", "period_end"}``,
        ``{"periods": [...]}``, or ``None`` when this kind does not select a
        period of its own (rank, percent, constant, composite, snapshot).
        """
        return None
