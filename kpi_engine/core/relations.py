"""Join monthly extracts from two models after each has been aggregated.

What this file provides
    join_monthly — pandas merge on model_relations.on (outer by default).

Where it is used
    orchestrator after per-model DuckDB extract + densify.

Capabilities
    Join keys missing from a frame (e.g. region on a G-only extract) are
    dropped automatically. how: outer | left | right | inner.

When to use
    KPI YAML model_relations. Do not SQL-join raw fact tables for ratio KPIs.
"""

from __future__ import annotations

import pandas as pd

from kpi_engine.contracts import KpiSpec
from kpi_engine.exceptions import BindError


def join_monthly(
    frames: dict[str, pd.DataFrame], kpi: KpiSpec
) -> pd.DataFrame:
    """Merge per-model monthly frames using KPI model_relations."""
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1 and not kpi.model_relations:
        return next(iter(frames.values()))
    if len(frames) > 1 and not kpi.model_relations:
        raise BindError(
            "Multiple model extracts need model_relations to join after aggregation."
        )
    by_measure = {m.name: (m.model_id or kpi.model_id) for m in kpi.base_measures}
    result: pd.DataFrame | None = None
    for rel in kpi.model_relations:
        left_id = by_measure.get(rel.left)
        right_id = by_measure.get(rel.right)
        if left_id not in frames or right_id not in frames:
            raise BindError(
                f"model_relations {rel.left}/{rel.right} missing extract "
                f"(have {sorted(frames)})."
            )
        left = result if result is not None and left_id in _present_models(result, kpi) else frames[left_id]
        right = frames[right_id]
        keys = [k for k in rel.on if k in left.columns and k in right.columns]
        if not keys:
            raise BindError(
                f"model_relations.on {list(rel.on)} are not in both extracts."
            )
        left = left.copy()
        right = right.copy()
        time_keys = [k for k in keys if "month" in k.lower() or k == "event_month"]
        for frame in (left, right):
            for col in keys:
                if col in time_keys or str(frame[col].dtype).startswith("datetime"):
                    frame[col] = pd.to_datetime(frame[col]).dt.normalize()
        right_keep = [*keys, *[c for c in right.columns if c not in left.columns]]
        how = rel.how if rel.how != "outer" else "outer"
        result = left.merge(right[right_keep], on=keys, how=how)
    return result if result is not None else next(iter(frames.values()))


def _present_models(frame: pd.DataFrame, kpi: KpiSpec) -> set[str]:
    """Model ids whose measure columns appear on the joined frame."""
    names = set(frame.columns)
    return {
        (m.model_id or kpi.model_id)
        for m in kpi.base_measures
        if m.name in names or f"{m.name}__sum" in names
    }
