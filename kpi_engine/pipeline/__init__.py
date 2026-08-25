"""Frozen pipeline package (adapt, bind, extract, dispatch).

What this file provides
    Namespace for adapter, binder, time_planner, filters, model_sql, cuts,
    calc_engine, orchestrator.

Where it is used
    kpi_engine.__init__ imports orchestrator from here. KPI authors should not
    import pipeline modules unless they are changing the engine.

Capabilities
    Implements the locked split: DuckDB retrieves model columns, Pandas runs KPI YAML.

When to use
    Edit a pipeline module when engine behaviour changes. For a new KPI, edit
    kpi_config/kpis/<kpi_group>/ instead.
"""
