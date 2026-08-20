"""UDF entrypoints for the existing metadata framework.

What this file provides
    Package for per-KPI shims. sotif.main is the current MEASURE UDF.

Where it is used
    Metadata calls module_path udfs.sotif.main (output_type df/JSON).

When to use
    Add another shim only if a legacy UDF name must stay. Prefer one generic
    entry later; keep shims one line that calls kpi_engine.compute.
"""
