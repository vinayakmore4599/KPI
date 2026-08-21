"""Core pipeline package (stable engine layer).

What this file provides
    Namespace for adapter, binder, time_planner, filters, model_sql, cuts,
    calc_engine, orchestrator.

Where it is used
    kpi_engine.__init__ imports orchestrator from here. KPI authors should not
    import core modules unless they are changing the engine.

Capabilities
    Implements the locked split: DuckDB extract + GROUP BY, Pandas calculations.

When to use
    Edit a core module when engine behaviour changes. For a new KPI, edit
    config/kpis/ instead.
"""
