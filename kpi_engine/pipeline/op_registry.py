"""In-memory OP_KINDS map. Loader fills it from registries/ops.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kpi_engine.exceptions import BindError, CatalogError

if TYPE_CHECKING:
    from kpi_engine.core.op_protocol import OpPlugin

OP_KINDS: dict[str, "OpPlugin"] = {}
_ALIASES: dict[str, str] = {}


def register_op(plugin: OpPlugin, *, aliases: tuple[str, ...] = ()) -> None:
    """Register a measure kind. Production code goes through the YAML loader."""
    names = (plugin.name, *plugin.aliases, *aliases)
    for name in names:
        if not name:
            continue
        OP_KINDS[name] = plugin
        _ALIASES[name] = plugin.name


def unregister_op(*names: str) -> None:
    """Remove kinds (tests). Also drops aliases that pointed at those names."""
    drop = set(names)
    for name in list(OP_KINDS):
        plugin = OP_KINDS[name]
        if name in drop or plugin.name in drop:
            OP_KINDS.pop(name, None)
            _ALIASES.pop(name, None)


def get_op(kind: str) -> OpPlugin:
    """Return the plugin for `kind`, loading registries on first use."""
    from kpi_engine.core.loader import ensure_loaded

    ensure_loaded()
    plugin = OP_KINDS.get(kind)
    if plugin is None:
        raise CatalogError(
            f"Unknown op/kind {kind!r}. Registered: {sorted(enabled_op_names())}."
        )
    return plugin


def enabled_op_names() -> list[str]:
    """Canonical kind names currently in memory."""
    return sorted({p.name for p in OP_KINDS.values()})


def require_op(kind: str, *, what: str = "measures") -> OpPlugin:
    """Bind-time lookup with BindError."""
    from kpi_engine.core.loader import ensure_loaded

    ensure_loaded()
    plugin = OP_KINDS.get(kind)
    if plugin is None:
        raise BindError(
            f"{what} has unknown op/kind {kind!r}. Registered: {sorted(enabled_op_names())}."
        )
    return plugin
