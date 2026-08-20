"""Reusable measure op kinds (catalog).

What this file provides
    Package marker. First-slice implementations are in core.calc_engine.
    ops.yaml lists kind names; ops_impl.py is a placeholder for a later split.

Where it is used
    Documentation and future extraction of op implementations from calc_engine.

When to use
    When adding an op, update ops.yaml kinds and calc_engine.evaluate. Do not
    put per-KPI formulas here — those belong in config/kpis/*.yaml.
"""
