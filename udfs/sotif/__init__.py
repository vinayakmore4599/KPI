"""Sotif UDF package. The callable entry is udfs.sotif.main.main.

What this file provides
    Re-exports main so `from udfs.sotif import main` and
    `from udfs.sotif.main import main` both work.

Where it is used
    Tests and hosts that import the function from the package.

When to use
    Do not put calculation logic here. Edit main.py only as a shim.
"""

from udfs.sotif.main import main

__all__ = ["main"]
