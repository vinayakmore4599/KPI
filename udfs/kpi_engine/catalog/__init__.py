"""Named functions KPI YAML may call (catalog).

What this file provides
    Package marker. ops_impl.py holds the two open registries: COLUMN_FNS for
    `base_measures.op` and MEASURE_FNS for `measures.fn`.

Where it is used
    Imported by binder (to validate names at bind time), calc_engine, and
    orchestrator. Project code registers into it via extensions.functions.

When to use
    Add a function here only if every KPI should have it. Anything
    project-specific belongs in extensions.functions, and per-KPI formulas
    belong in config/kpis/*.yaml. The measure op kinds themselves (point,
    window, arithmetic, trend, fn, dimension, hook) are dispatched in
    calc_engine.evaluate and documented in kpi-yaml-reference.md.
"""
