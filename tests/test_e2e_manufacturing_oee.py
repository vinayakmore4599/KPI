"""End-to-end: plant OEE from a shift log, the hardest shape the engine has to hold.

What this file provides
    OEE = Availability × Performance × Quality is the standard case that breaks
    naive KPI engines: each factor is a ratio of two *sums*, and the product of
    those ratios is not any row-level formula and not the mean of the per-line
    OEEs. This file proves the engine gets it right without help from SQL:
      L1  shift totals   — planned / run / downtime / units / good, per line
      L2  factors        — availability, performance, quality as ratio-of-sums
      L3  composite      — OEE and OEE% from the three factors
      L4  the trap       — plant OEE is the ratio of sums, NOT the mean of ratios
      L5  month on month — the composite re-derived at the prior anchor
      L6  rolling 6m     — window sums first, ratio second
      L7  downtime       — Pareto of stoppage reasons (rank, share, cumulative)
      L8  governance     — world-class flags, gap to the 85% target, green_when

    The model YAML is a plain physical extract with no expressions, so every
    derivation below is done by the KPI engine. Numbers are checked against a
    pandas oracle and against hand-computed constants for 2026-03.

Where it is used
    pytest tests/test_e2e_manufacturing_oee.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_engine import compute, validate
from kpi_engine.pipeline.binder import load_kpi
from kpi_engine.pipeline.time_planner import lookback_for, lookforward_for
from kpi_engine.dates import month_range_inclusive
from tests.conftest import make_context, write_yaml

ANCHOR = date(2026, 3, 1)
MONTHS = month_range_inclusive(date(2025, 7, 1), ANCHOR)
LAST = len(MONTHS) - 1

# Continuous-improvement drift: less downtime, more units, fewer rejects.
DOWN_DRIFT = (40, 35, 30, 28, 25, 22, 20, 18, 15)
UNIT_DRIFT = (0, 10, 20, 15, 25, 30, 35, 40, 50)
REJECT_DRIFT = (6, 5, 5, 4, 4, 3, 3, 2, 2)

# Working days per calendar month. Uneven months are what makes a rolling OEE
# differ from the mean of the monthly OEEs, so they must not be flattened away.
WORK_DAYS = (22, 21, 23, 20, 22, 19, 21, 20, 22)

# plant, line, shift, planned_min, base_down, cycle_sec, base_units, base_rej, reason, stops
SHIFTS = (
    ("P1", "L1", "A", 480, 10, 30, 800, 4, "Changeover", 3),
    ("P1", "L1", "B", 480, 15, 30, 780, 5, "Breakdown", 2),
    ("P1", "L1", "C", 480, 5, 30, 760, 3, "Minor Stop", 5),
    ("P1", "L2", "A", 480, 20, 36, 640, 6, "Breakdown", 4),
    ("P1", "L2", "B", 480, 12, 36, 620, 4, "Changeover", 3),
    ("P1", "L2", "C", 480, 8, 36, 600, 5, "Setup", 2),
    ("P2", "L3", "A", 450, 25, 45, 480, 7, "Breakdown", 6),
    ("P2", "L3", "B", 450, 18, 45, 470, 6, "Material", 3),
    ("P2", "L3", "C", 450, 10, 45, 460, 4, "Minor Stop", 4),
)

FACT_COLUMNS = [
    "event_month",
    "plant",
    "line",
    "shift",
    "downtime_reason",
    "planned_minutes",
    "downtime_minutes",
    "ideal_cycle_sec",
    "total_units",
    "reject_units",
    "stop_events",
]

OEE_TARGET_PCT = 85.0

L1_MEASURES = [
    "planned_minutes",
    "downtime_minutes",
    "run_minutes",
    "total_units",
    "reject_units",
    "good_units",
    "ideal_minutes",
]
L2_MEASURES = ["availability", "performance", "quality", "downtime_rate", "scrap_rate", "mtbf"]
L3_MEASURES = ["availability", "performance", "quality", "oee", "oee_pct"]
L5_MEASURES = ["oee_pct", "prior_oee_pct", "oee_delta_points"]
L6_MEASURES = [
    "oee_pct",
    "availability_6m",
    "performance_6m",
    "quality_6m",
    "oee_6m_pct",
    "prior_oee_6m_pct",
]
L7_MEASURES = ["downtime_minutes", "downtime_share", "downtime_rank", "downtime_pareto"]
L8_MEASURES = [
    "oee_pct",
    "availability",
    "performance",
    "quality",
    "world_class",
    "oee_gap_points",
    "meets_availability",
    "oee_zscore",
    "oee_rank",
]


# --------------------------------------------------------------------------
# shift log
# --------------------------------------------------------------------------


def _rows() -> list[dict]:
    """One row per month × line × shift, scaled by that month's working days."""
    out: list[dict] = []
    for idx, month in enumerate(MONTHS):
        days = WORK_DAYS[idx]
        for plant, line, shift, planned, down, cycle, units, rejects, reason, stops in SHIFTS:
            out.append(
                {
                    "event_month": month,
                    "plant": plant,
                    "line": line,
                    "shift": shift,
                    "downtime_reason": reason,
                    "planned_minutes": float(planned * days),
                    "downtime_minutes": float((down + DOWN_DRIFT[idx]) * days),
                    "ideal_cycle_sec": float(cycle),
                    "total_units": float((units + UNIT_DRIFT[idx]) * days),
                    "reject_units": float((rejects + REJECT_DRIFT[idx]) * days),
                    "stop_events": float(stops * days),
                }
            )
    return out


def _frame() -> pd.DataFrame:
    frame = pd.DataFrame(_rows())[FACT_COLUMNS]
    frame["run_minutes"] = frame["planned_minutes"] - frame["downtime_minutes"]
    frame["good_units"] = frame["total_units"] - frame["reject_units"]
    frame["ideal_minutes"] = frame["total_units"] * frame["ideal_cycle_sec"] / 60.0
    return frame


def _parquet(tmp_path):
    path = tmp_path / "shift_log.parquet"
    _frame()[FACT_COLUMNS].to_parquet(path, index=False)
    return path


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------

SUMMED = (
    "planned_minutes",
    "downtime_minutes",
    "run_minutes",
    "total_units",
    "reject_units",
    "good_units",
    "ideal_minutes",
    "stop_events",
)


def _sums(chunk: pd.DataFrame) -> dict[str, float]:
    return {name: float(chunk[name].sum()) for name in SUMMED}


def _factors(totals: dict[str, float]) -> dict[str, float]:
    """Availability / Performance / Quality / OEE from already-summed inputs."""
    availability = totals["run_minutes"] / totals["planned_minutes"]
    performance = totals["ideal_minutes"] / totals["run_minutes"]
    quality = totals["good_units"] / totals["total_units"]
    return {
        **totals,
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": availability * performance * quality,
        "oee_pct": availability * performance * quality * 100.0,
    }


def _by_grain(months, keys: tuple[str, ...]) -> dict[tuple, dict]:
    """Oracle factors per grain cell over the given months."""
    frame = _frame()
    stamped = pd.to_datetime(frame["event_month"]).dt.date
    chunk = frame[stamped.isin(set(months))]
    if not keys:
        return {(): _factors(_sums(chunk))}
    out: dict[tuple, dict] = {}
    for name, part in chunk.groupby(list(keys), dropna=False):
        key = name if isinstance(name, tuple) else (name,)
        out[key] = _factors(_sums(part))
    return out


def _downtime_by_reason(month: date) -> dict[str, float]:
    frame = _frame()
    stamped = pd.to_datetime(frame["event_month"]).dt.date
    chunk = frame[stamped == month]
    grouped = chunk.groupby("downtime_reason")["downtime_minutes"].sum()
    return {str(k): float(v) for k, v in grouped.items()}


def _zscores(values: list[float]) -> list[float]:
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    stdev = var**0.5
    return [(v - mean) / stdev for v in values]


# --------------------------------------------------------------------------
# KPI YAML
# --------------------------------------------------------------------------


def _base_measures() -> dict:
    return {
        "planned_min": {"sql": "planned_minutes", "agg": "sum"},
        "downtime_min": {"sql": "downtime_minutes", "agg": "sum"},
        "run_min": {"expr": "planned_minutes - downtime_minutes", "agg": "sum"},
        "unit_count": {"sql": "total_units", "agg": "sum"},
        "reject_count": {"sql": "reject_units", "agg": "sum"},
        "good_count": {"expr": "total_units - reject_units", "agg": "sum"},
        "ideal_min": {"expr": "total_units * ideal_cycle_sec / 60", "agg": "sum"},
        "stop_count": {"sql": "stop_events", "agg": "sum"},
    }


def _measures() -> dict:
    now = {"months": 0}
    prior = {"months": 1}
    six = {"months": 6}
    return {
        # L1 raw totals
        "planned_minutes": {"of": "planned_min", "op": "point", "offset": now},
        "downtime_minutes": {"of": "downtime_min", "op": "point", "offset": now},
        "run_minutes": {"of": "run_min", "op": "point", "offset": now},
        "total_units": {"of": "unit_count", "op": "point", "offset": now},
        "reject_units": {"of": "reject_count", "op": "point", "offset": now},
        "good_units": {"of": "good_count", "op": "point", "offset": now},
        "ideal_minutes": {"of": "ideal_min", "op": "point", "offset": now},
        "stoppages": {"of": "stop_count", "op": "point", "offset": now},
        # L2 the three OEE factors, each a ratio of two sums
        "availability": {"op": "fn", "fn": "divide", "inputs": ["run_minutes", "planned_minutes"]},
        "performance": {"op": "fn", "fn": "divide", "inputs": ["ideal_minutes", "run_minutes"]},
        "quality": {"op": "fn", "fn": "divide", "inputs": ["good_units", "total_units"]},
        "downtime_rate": {
            "op": "fn",
            "fn": "percent",
            "inputs": ["downtime_minutes", "planned_minutes"],
        },
        "scrap_rate": {"op": "fn", "fn": "percent", "inputs": ["reject_units", "total_units"]},
        "mtbf": {"op": "fn", "fn": "divide", "inputs": ["run_minutes", "stoppages"]},
        # L3 the composite
        "oee": {
            "op": "fn",
            "fn": "multiply",
            "inputs": ["availability", "performance", "quality"],
        },
        "oee_pct": {"op": "expr", "expr": "oee * 100"},
        # L5: lag/diff of the composite (not a duplicated prior_* subgraph)
        "prior_oee_pct": {"op": "lag", "of": "oee_pct", "offset": prior},
        "oee_delta_points": {"op": "diff", "of": "oee_pct", "offset": prior},
        "next_oee_pct": {"op": "lead", "of": "oee_pct", "offset": prior},
        # L6 rolling six months: sum the window first, divide second
        "planned_6m": {"of": "planned_min", "op": "window", "trailing": six, "inclusive": True},
        "run_6m": {"of": "run_min", "op": "window", "trailing": six, "inclusive": True},
        "ideal_6m": {"of": "ideal_min", "op": "window", "trailing": six, "inclusive": True},
        "units_6m": {"of": "unit_count", "op": "window", "trailing": six, "inclusive": True},
        "good_6m": {"of": "good_count", "op": "window", "trailing": six, "inclusive": True},
        "availability_6m": {"op": "fn", "fn": "divide", "inputs": ["run_6m", "planned_6m"]},
        "performance_6m": {"op": "fn", "fn": "divide", "inputs": ["ideal_6m", "run_6m"]},
        "quality_6m": {"op": "fn", "fn": "divide", "inputs": ["good_6m", "units_6m"]},
        "oee_6m": {
            "op": "fn",
            "fn": "multiply",
            "inputs": ["availability_6m", "performance_6m", "quality_6m"],
        },
        "oee_6m_pct": {"op": "expr", "expr": "oee_6m * 100"},
        "prior_oee_6m_pct": {"op": "lag", "of": "oee_6m_pct", "offset": prior},
        # L7 downtime Pareto
        "downtime_share": {"op": "percent_of_total", "of": "downtime_minutes", "cuts": ["G"]},
        "downtime_rank": {
            "op": "rank",
            "of": "downtime_minutes",
            "order": "desc",
            "cuts": ["G"],
        },
        "downtime_pareto": {
            "op": "cumulative_share",
            "of": "downtime_minutes",
            "order": "desc",
            "cuts": ["G"],
        },
        # L8 governance
        "world_class": {
            "op": "predicate",
            "match": "all",
            "predicates": [
                {"of": "availability", "cmp": "gte", "value": 0.90},
                {"of": "performance", "cmp": "gte", "value": 0.95},
                {"of": "quality", "cmp": "gte", "value": 0.999},
            ],
        },
        "oee_gap_points": {"op": "vs_target", "of": "oee_pct", "value": OEE_TARGET_PCT, "as": "gap"},
        "meets_availability": {
            "op": "threshold",
            "of": "availability",
            "cmp": "gte",
            "value": 0.90,
        },
        "oee_zscore": {"op": "zscore", "of": "oee_pct", "cuts": ["G"]},
        "oee_rank": {"op": "rank", "of": "oee_pct", "order": "desc", "cuts": ["G"]},
    }


def _oee_kpi(kpi_id: int) -> dict:
    return {
        "kpi_id": kpi_id,
        "version": 1,
        "model": "shift_log",
        "time": {
            "column": "event_month",
            "grain": "month",
            "filter_code": "reporting_month",
            "calendar": "gregorian",
        },
        "dimensions": [
            {"name": "plant", "from": "plant"},
            {"name": "line", "from": "line"},
            {"name": "shift", "from": "shift"},
            {"name": "downtime_reason", "from": "downtime_reason"},
        ],
        "default_dimensions": ["line"],
        "base_measures": _base_measures(),
        "cuts": [{"name": "G", "group_by": [], "ignore_filters": []}],
        "default_cut": "G",
        "measures": _measures(),
        "green_when": {"of": "oee_pct", "above": OEE_TARGET_PCT},
    }


def _model() -> dict:
    return {
        "model_id": "shift_log",
        "kind": "physical",
        "required_aliases": ["shift_log"],
        "sources": {"shift_log": {"alias": "shift_log"}},
        "joins": [],
    }


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _setup(tmp_path, extra_config, kpi_id: int, measures: list[str], **kwargs):
    write_yaml(extra_config / "models" / "manufacturing" / "shift_log.yaml", _model())
    write_yaml(extra_config / "kpis" / "manufacturing" / f"{kpi_id}.yaml", _oee_kpi(kpi_id))
    ctx = make_context(
        _parquet(tmp_path),
        measures=measures,
        kpi_id=kpi_id,
        month=kwargs.pop("month", "2026-03"),
        **kwargs,
    )
    ctx["datasets"]["Sotif"]["columns"] = list(FACT_COLUMNS)
    ctx["datasets"]["Sotif"]["alias"] = "shift_log"
    return ctx


def _run(ctx, extra_config) -> dict:
    planned = validate(ctx, config_dir=extra_config)
    result = compute(ctx, config_dir=extra_config)
    assert planned["ok"] is True, planned
    assert planned["sql"] == result["sql"]
    return result


def _pick(result: dict, **dims) -> dict:
    matches = [
        row
        for row in result["rows"]
        if row.get("output_cut") == "G"
        and all(row.get(name) == value for name, value in dims.items())
    ]
    assert matches, (dims, result["rows"])
    assert len(matches) == 1, (dims, matches)
    return matches[0]


# --------------------------------------------------------------------------
# L1 — shift log totals
# --------------------------------------------------------------------------


def test_oee_l1_shift_totals_per_line(tmp_path, extra_config):
    """Row helpers (run time, good units, ideal minutes) summed per line."""
    ctx = _setup(tmp_path, extra_config, 9701, L1_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    expected = _by_grain([ANCHOR], ("line",))
    assert len(result["rows"]) == 3
    for (line,), want in expected.items():
        row = _pick(result, line=line)
        for name in L1_MEASURES:
            assert row[name] == pytest.approx(want[name]), (line, name)

    # March 2026 ran 22 days across L1's three 480-minute shifts.
    l1 = _pick(result, line="L1")
    assert l1["planned_minutes"] == pytest.approx(1440.0 * 22)
    assert l1["downtime_minutes"] == pytest.approx((25.0 + 30.0 + 20.0) * 22)
    assert l1["run_minutes"] == pytest.approx((1440.0 - 75.0) * 22)
    assert l1["total_units"] == pytest.approx((850.0 + 830.0 + 810.0) * 22)
    assert l1["good_units"] == pytest.approx((844.0 + 823.0 + 805.0) * 22)
    # 30s ideal cycle: 54780 units should take 27390 minutes of perfect running.
    assert l1["ideal_minutes"] == pytest.approx(1245.0 * 22)


# --------------------------------------------------------------------------
# L2 — the three factors
# --------------------------------------------------------------------------


def test_oee_l2_component_factors(tmp_path, extra_config):
    """Availability, performance, quality plus downtime %, scrap % and MTBF."""
    ctx = _setup(tmp_path, extra_config, 9702, L2_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    for (line,), want in _by_grain([ANCHOR], ("line",)).items():
        row = _pick(result, line=line)
        assert row["availability"] == pytest.approx(want["availability"])
        assert row["performance"] == pytest.approx(want["performance"])
        assert row["quality"] == pytest.approx(want["quality"])
        assert row["downtime_rate"] == pytest.approx(
            want["downtime_minutes"] * 100.0 / want["planned_minutes"]
        )
        assert row["scrap_rate"] == pytest.approx(
            want["reject_units"] * 100.0 / want["total_units"]
        )
        assert row["mtbf"] == pytest.approx(want["run_minutes"] / want["stop_events"])

    l1 = _pick(result, line="L1")
    assert l1["availability"] == pytest.approx(1365.0 / 1440.0)
    assert l1["performance"] == pytest.approx(1245.0 / 1365.0)
    assert l1["quality"] == pytest.approx(2472.0 / 2490.0)
    assert l1["mtbf"] == pytest.approx(1365.0 / 10.0)
    assert l1["downtime_rate"] == pytest.approx(75.0 * 100.0 / 1440.0)


# --------------------------------------------------------------------------
# L3 — the composite
# --------------------------------------------------------------------------


def test_oee_l3_composite_oee(tmp_path, extra_config):
    """OEE is the product of the three factors, per line and hand-checked on L1."""
    ctx = _setup(tmp_path, extra_config, 9703, L3_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    for (line,), want in _by_grain([ANCHOR], ("line",)).items():
        row = _pick(result, line=line)
        assert row["oee"] == pytest.approx(want["oee"])
        assert row["oee_pct"] == pytest.approx(want["oee"] * 100.0)
        assert row["oee"] == pytest.approx(
            row["availability"] * row["performance"] * row["quality"]
        )

    l1 = _pick(result, line="L1")
    assert l1["oee"] == pytest.approx((1365.0 / 1440.0) * (1245.0 / 1365.0) * (2472.0 / 2490.0))
    # The three-factor product collapses to good minutes over planned minutes.
    assert l1["oee"] == pytest.approx(1245.0 * (2472.0 / 2490.0) / 1440.0)

    ranked = sorted(result["rows"], key=lambda row: -row["oee_pct"])
    assert [row["line"] for row in ranked] == ["L1", "L3", "L2"]


# --------------------------------------------------------------------------
# L4 — ratio of sums, not mean of ratios
# --------------------------------------------------------------------------


def test_oee_l4_rollup_is_ratio_of_sums_not_mean_of_ratios(tmp_path, extra_config):
    """The classic OEE rollup error. Plant and company OEE re-aggregate the inputs."""
    line_ctx = _setup(tmp_path, extra_config, 9704, L3_MEASURES, selected_dimensions=["line"])
    line_result = _run(line_ctx, extra_config)
    per_line = {row["line"]: row["oee"] for row in line_result["rows"]}

    plant_ctx = _setup(tmp_path, extra_config, 9705, L3_MEASURES, selected_dimensions=["plant"])
    plant_result = _run(plant_ctx, extra_config)
    for (plant,), want in _by_grain([ANCHOR], ("plant",)).items():
        assert _pick(plant_result, plant=plant)["oee"] == pytest.approx(want["oee"])

    total_ctx = _setup(tmp_path, extra_config, 9706, L3_MEASURES, selected_dimensions=[])
    total_result = _run(total_ctx, extra_config)
    assert len(total_result["rows"]) == 1
    company = total_result["rows"][0]
    truth = _by_grain([ANCHOR], ())[()]
    assert company["oee"] == pytest.approx(truth["oee"])
    assert company["availability"] == pytest.approx(3972.0 / 4230.0)
    assert company["performance"] == pytest.approx(3621.0 / 3972.0)
    assert company["quality"] == pytest.approx(5998.0 / 6060.0)

    # The whole point: averaging the line OEEs gives a different, wrong number.
    mean_of_ratios = sum(per_line.values()) / len(per_line)
    assert company["oee"] != pytest.approx(mean_of_ratios)
    assert abs(company["oee"] - mean_of_ratios) > 1e-5


# --------------------------------------------------------------------------
# L5 — month over month on a composite
# --------------------------------------------------------------------------


def test_oee_l5_month_over_month_points(tmp_path, extra_config):
    """lag/diff of OEE% equals rebuilding the composite at the prior month."""
    ctx = _setup(tmp_path, extra_config, 9707, L5_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    now = _by_grain([ANCHOR], ("line",))
    before = _by_grain([MONTHS[LAST - 1]], ("line",))
    for (line,), want in now.items():
        row = _pick(result, line=line)
        previous = before[(line,)]
        assert row["oee_pct"] == pytest.approx(want["oee_pct"])
        assert row["prior_oee_pct"] == pytest.approx(previous["oee_pct"])
        assert row["oee_delta_points"] == pytest.approx(
            want["oee_pct"] - previous["oee_pct"]
        )
        # Downtime falls and output rises every month, so OEE improves.
        assert row["oee_delta_points"] > 0


def test_oee_lead_of_composite_looks_forward(tmp_path, extra_config):
    """lead of OEE% at February is March's composite, not February's (lookforward)."""
    feb = date(2026, 2, 1)
    ctx = _setup(
        tmp_path,
        extra_config,
        9711,
        ["oee_pct", "next_oee_pct"],
        selected_dimensions=["line"],
        month="2026-02",
    )
    result = _run(ctx, extra_config)

    now = _by_grain([feb], ("line",))
    nxt = _by_grain([ANCHOR], ("line",))
    for (line,), want in now.items():
        row = _pick(result, line=line)
        assert row["oee_pct"] == pytest.approx(want["oee_pct"])
        assert row["next_oee_pct"] == pytest.approx(nxt[(line,)]["oee_pct"])
        assert row["next_oee_pct"] != pytest.approx(row["oee_pct"])


def test_oee_shift_planner_widens_window_plus_offset(tmp_path, extra_config):
    """lag of a 6m window needs window+offset lookback; lead of a composite looks forward."""
    _setup(
        tmp_path,
        extra_config,
        9712,
        ["prior_oee_pct", "prior_oee_6m_pct", "next_oee_pct"],
        selected_dimensions=["line"],
    )
    kpi = load_kpi(9712, extra_config)
    by_key = {m.key: m for m in kpi.measures}
    assert lookback_for(by_key["prior_oee_pct"], by_key, kpi.time) == 1
    assert lookback_for(by_key["oee_6m_pct"], by_key, kpi.time) == 5
    assert lookback_for(by_key["prior_oee_6m_pct"], by_key, kpi.time) == 6
    assert lookforward_for(by_key["next_oee_pct"], by_key, time=kpi.time) == 1


# --------------------------------------------------------------------------
# L6 — rolling window
# --------------------------------------------------------------------------


def test_oee_l6_rolling_six_month_oee(tmp_path, extra_config):
    """Rolling OEE sums the window inputs first; it is not the mean of six OEEs."""
    ctx = _setup(tmp_path, extra_config, 9708, L6_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    window = MONTHS[LAST - 5 : LAST + 1]
    assert len(window) == 6
    rolled = _by_grain(window, ("line",))
    monthly = [_by_grain([month], ("line",)) for month in window]

    for (line,), want in rolled.items():
        row = _pick(result, line=line)
        assert row["availability_6m"] == pytest.approx(want["availability"])
        assert row["performance_6m"] == pytest.approx(want["performance"])
        assert row["quality_6m"] == pytest.approx(want["quality"])
        assert row["oee_6m_pct"] == pytest.approx(want["oee_pct"])

        mean_of_months = sum(m[(line,)]["oee_pct"] for m in monthly) / 6.0
        assert row["oee_6m_pct"] != pytest.approx(mean_of_months)
        # The window is still below the improving anchor month.
        assert row["oee_6m_pct"] < row["oee_pct"]

    prior_window = MONTHS[LAST - 6 : LAST]
    assert len(prior_window) == 6
    prior_rolled = _by_grain(prior_window, ("line",))
    for (line,), want in prior_rolled.items():
        row = _pick(result, line=line)
        assert row["prior_oee_6m_pct"] == pytest.approx(want["oee_pct"])
        assert row["prior_oee_6m_pct"] != pytest.approx(row["oee_6m_pct"])


# --------------------------------------------------------------------------
# L7 — downtime Pareto
# --------------------------------------------------------------------------


def test_oee_l7_downtime_pareto_by_reason(tmp_path, extra_config):
    """Stoppage reasons ranked, shared and accumulated to 100%."""
    ctx = _setup(
        tmp_path,
        extra_config,
        9709,
        L7_MEASURES,
        selected_dimensions=["downtime_reason"],
    )
    result = _run(ctx, extra_config)

    minutes = _downtime_by_reason(ANCHOR)
    grand = sum(minutes.values())
    ordered = sorted(minutes.items(), key=lambda item: -item[1])
    assert len(result["rows"]) == len(minutes) == 5

    running = 0.0
    for position, (reason, value) in enumerate(ordered, start=1):
        running += value
        row = _pick(result, downtime_reason=reason)
        assert row["downtime_minutes"] == pytest.approx(value)
        assert row["downtime_rank"] == position
        assert row["downtime_share"] == pytest.approx(value * 100.0 / grand)
        assert row["downtime_pareto"] == pytest.approx(running * 100.0 / grand)

    # Breakdown leads: (30 + 35 + 40) minutes a day across three lines, 22 days.
    assert ordered[0][0] == "Breakdown"
    assert _pick(result, downtime_reason="Breakdown")["downtime_minutes"] == pytest.approx(
        105.0 * 22
    )
    assert _pick(result, downtime_reason=ordered[-1][0])["downtime_pareto"] == pytest.approx(100.0)


# --------------------------------------------------------------------------
# L8 — governance
# --------------------------------------------------------------------------


def test_oee_l8_world_class_flags_and_target_gap(tmp_path, extra_config):
    """World-class predicate, gap to the 85% target, z-score and green_when."""
    ctx = _setup(tmp_path, extra_config, 9710, L8_MEASURES, selected_dimensions=["line"])
    result = _run(ctx, extra_config)

    expected = _by_grain([ANCHOR], ("line",))
    for (line,), want in expected.items():
        row = _pick(result, line=line)
        world_class = (
            want["availability"] >= 0.90
            and want["performance"] >= 0.95
            and want["quality"] >= 0.999
        )
        assert row["world_class"] == pytest.approx(1.0 if world_class else 0.0)
        assert row["oee_gap_points"] == pytest.approx(want["oee_pct"] - OEE_TARGET_PCT)
        assert row["meets_availability"] == pytest.approx(
            1.0 if want["availability"] >= 0.90 else 0.0
        )
        assert row["green"] is (want["oee_pct"] >= OEE_TARGET_PCT)

    ordered = sorted(expected.items(), key=lambda item: -item[1]["oee_pct"])
    for position, ((line,), _want) in enumerate(ordered, start=1):
        assert _pick(result, line=line)["oee_rank"] == position

    scores = _zscores([want["oee_pct"] for (_line,), want in expected.items()])
    for ((line,), _want), score in zip(expected.items(), scores):
        assert _pick(result, line=line)["oee_zscore"] == pytest.approx(score)

    # Every line clears 90% availability but none reaches 99.9% quality,
    # so world_class is uniformly 0 while L1 is still green on OEE.
    assert all(row["meets_availability"] == pytest.approx(1.0) for row in result["rows"])
    assert all(row["world_class"] == pytest.approx(0.0) for row in result["rows"])
    assert _pick(result, line="L1")["green"] is True
    assert _pick(result, line="L2")["green"] is False
