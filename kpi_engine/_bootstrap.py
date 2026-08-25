"""Put the parent of this package on sys.path. Never the package directory."""

from __future__ import annotations

import sys
from pathlib import Path

from kpi_engine.exceptions import CatalogError


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def _resolved_path_entries(path_entries: list[str]) -> set[Path]:
    out: set[Path] = set()
    for item in path_entries:
        if not item:
            out.add(Path.cwd().resolve())
        else:
            out.add(Path(item).resolve())
    return out


def assert_sys_path_safe(pkg: Path, path_entries: list[str] | None = None) -> None:
    """Reject adding kpi_engine/ itself to sys.path (Hub core / pipeline shadow)."""
    entries = sys.path if path_entries is None else path_entries
    pkg_r = pkg.resolve()
    if pkg_r in _resolved_path_entries(list(entries)):
        raise CatalogError(
            f"{pkg_r} is on sys.path. Put its parent on sys.path so "
            "`import kpi_engine` works; do not add the kpi_engine/ folder "
            "itself (that shadows Hub top-level packages such as core)."
        )


def ensure_parent_on_path(pkg: Path | None = None) -> None:
    pkg = (pkg or package_dir()).resolve()
    assert_sys_path_safe(pkg)
    root = str(pkg.parent)
    if root not in sys.path:
        sys.path.append(root)
