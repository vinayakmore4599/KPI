"""Named-hook extension layer (soft layer).

What this file provides
    Package marker. Hook implementations live in hooks.py.

Where it is used
    Only when a KPI cannot be expressed as point/window/trend/arithmetic.

When to use
    Add a module here for a new hook family; keep DuckDB and ADLS out of this
    package — hooks receive already-aligned frames.
"""
