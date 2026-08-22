"""Load capability registries once at process start."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from kpi_engine.exceptions import BindError, CatalogError

MODULE_PREFIX = "kpi_engine.capabilities."
REGISTRY_FILES = (
    ("column_fn", "functions/column.yaml"),
    ("measure_fn", "functions/measure.yaml"),
    ("op", "ops.yaml"),
    ("hook", "hooks.yaml"),
)

_loaded = False
_loading = False
_entries: list[dict[str, Any]] = []
_skipped_addons: dict[str, str] = {}


def registries_dir() -> Path:
    """Packaged YAML allowlist."""
    return Path(__file__).resolve().parents[1] / "registries"


def ensure_loaded(entries: list[dict[str, Any]] | None = None) -> None:
    """Load packaged registries, or an injected list (tests). Reentrant."""
    global _loaded, _loading
    if entries is not None:
        _reset()
        _load_entries(entries)
        _loaded = True
        return
    if _loaded or _loading:
        return
    _loading = True
    try:
        _load_entries(_read_packaged())
        _loaded = True
    finally:
        _loading = False


def reload_packaged() -> None:
    """Drop in-memory maps and load the four packaged YAML files again."""
    _reset()
    ensure_loaded()


def _reset() -> None:
    global _loaded, _entries, _skipped_addons
    from kpi_engine.catalog.ops_impl import COLUMN_FNS, MEASURE_FNS, _COLUMN_META, _MEASURE_META
    from kpi_engine.core.op_registry import OP_KINDS, _ALIASES
    from kpi_engine.extensions.hooks import REGISTRY

    COLUMN_FNS.clear()
    MEASURE_FNS.clear()
    _COLUMN_META.clear()
    _MEASURE_META.clear()
    OP_KINDS.clear()
    _ALIASES.clear()
    # Keep test-registered hooks; only clear names that came from YAML.
    for item in _entries:
        if item.get("type") == "hook":
            REGISTRY.pop(item["name"], None)
            for alias in item.get("aliases") or []:
                REGISTRY.pop(alias, None)
    _entries = []
    _skipped_addons = {}
    _loaded = False


def _read_packaged() -> list[dict[str, Any]]:
    root = registries_dir()
    out: list[dict[str, Any]] = []
    for kind, rel in REGISTRY_FILES:
        path = root / rel
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise CatalogError(f"Registry {rel} must be a mapping of name → spec.")
        for name, spec in raw.items():
            if spec is None:
                spec = {}
            if not isinstance(spec, dict):
                raise CatalogError(f"Registry {rel} entry {name!r} must be an object.")
            out.append(_normalize(kind, str(name), spec, source=rel))
    return out


def _normalize(kind: str, name: str, spec: dict[str, Any], *, source: str) -> dict[str, Any]:
    role = str(spec.get("role") or "addon")
    if role not in {"platform", "addon"}:
        raise CatalogError(f"{source} {name!r} role must be platform or addon.")
    enabled = spec.get("enabled", True)
    if enabled not in {True, False}:
        raise CatalogError(f"{source} {name!r} enabled must be true or false.")
    aliases = spec.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    description = str(spec.get("description") or "").strip()
    example = str(spec.get("example") or "").strip()
    if not description or not example:
        raise CatalogError(f"{source} {name!r} needs description and example.")
    return {
        "type": kind,
        "name": name,
        "role": role,
        "enabled": bool(enabled),
        "aliases": tuple(str(a) for a in aliases),
        "description": description,
        "example": example,
        "module": spec.get("module"),
        "attr": spec.get("attr"),
        "source": source,
    }


def _load_entries(entries: list[dict[str, Any]]) -> None:
    global _entries, _skipped_addons
    _entries = list(entries)
    _skipped_addons = {}
    seen: set[tuple[str, str]] = set()
    for item in _entries:
        key = (item["type"], item["name"])
        if key in seen:
            raise CatalogError(f"Duplicate registry name {item['name']!r} in {item['type']}.")
        seen.add(key)
        if item["role"] == "platform" and not item["enabled"]:
            raise CatalogError(
                f"Platform capability {item['name']!r} cannot have enabled: false "
                f"({item['source']})."
            )
        if not item["enabled"]:
            continue
        try:
            _register_one(item)
        except Exception as exc:
            if item["role"] == "platform":
                raise CatalogError(
                    f"Platform capability {item['name']!r} failed to load: {exc}"
                ) from exc
            _skipped_addons[item["name"]] = str(exc)


def _register_one(item: dict[str, Any]) -> None:
    module_name = item.get("module")
    attr_name = item.get("attr")
    if not module_name or not attr_name:
        raise CatalogError(f"{item['name']!r} needs module and attr.")
    if not str(module_name).startswith(MODULE_PREFIX):
        raise CatalogError(
            f"{item['name']!r} module {module_name!r} must start with {MODULE_PREFIX!r}."
        )
    module = importlib.import_module(str(module_name))
    obj = getattr(module, str(attr_name), None)
    if obj is None:
        raise CatalogError(
            f"{item['name']!r} attr {attr_name!r} not found on {module_name}."
        )
    kind = item["type"]
    aliases = item["aliases"]
    if kind == "column_fn":
        from kpi_engine.catalog.ops_impl import register_column_fn

        if not callable(obj):
            raise CatalogError(f"{item['name']!r} column function must be callable.")
        min_columns = 2 if item["name"] in {"sum", "subtract", "multiply", "min", "max", "avg", "coalesce"} else None
        register_column_fn(item["name"], obj, min_columns=min_columns, aliases=aliases)
        return
    if kind == "measure_fn":
        from kpi_engine.catalog.ops_impl import register_measure_fn

        if not callable(obj):
            raise CatalogError(f"{item['name']!r} measure function must be callable.")
        min_inputs = 2 if item["name"] in {"sum", "subtract", "multiply", "min", "max", "avg"} else None
        register_measure_fn(item["name"], obj, min_inputs=min_inputs, aliases=aliases)
        return
    if kind == "hook":
        from kpi_engine.extensions.hooks import register

        if not callable(obj):
            raise CatalogError(f"{item['name']!r} hook must be callable.")
        register(item["name"], obj)
        for alias in aliases:
            register(alias, obj)
        return
    if kind == "op":
        from kpi_engine.core.op_protocol import OpPlugin
        from kpi_engine.core.op_registry import register_op

        plugin: OpPlugin
        if isinstance(obj, type) and issubclass(obj, OpPlugin):
            plugin = obj()
        elif isinstance(obj, OpPlugin):
            plugin = obj
        else:
            raise CatalogError(f"{item['name']!r} attr must be an OpPlugin class or instance.")
        if plugin.name and plugin.name != item["name"]:
            plugin.name = item["name"]  # type: ignore[misc]
        register_op(plugin, aliases=aliases)
        return
    raise CatalogError(f"Unknown registry type {kind!r}.")


def list_capabilities() -> list[dict[str, Any]]:
    """Every packaged registry row, including disabled add-ons."""
    rows = _read_packaged() if not _entries else list(_entries)
    if not _entries:
        rows = _read_packaged()
    # Always prefer packaged YAML for discoverability.
    rows = _read_packaged()
    from kpi_engine.core.op_registry import OP_KINDS

    out = []
    for item in rows:
        row = {
            "type": item["type"],
            "name": item["name"],
            "aliases": list(item["aliases"]),
            "description": item["description"],
            "example": item["example"],
            "enabled": item["enabled"],
            "role": item["role"],
        }
        plugin = OP_KINDS.get(item["name"])
        if plugin is not None:
            row["phase"] = plugin.phase
            row["cut_restricted"] = plugin.cut_restricted
            row["requires_time"] = plugin.requires_time
        out.append(row)
    return out


def skipped_addons() -> Mapping[str, str]:
    """Add-on names that failed to load, and why."""
    return dict(_skipped_addons)


def assert_named_capability(kind: str, name: str, *, what: str) -> None:
    """Bind-time: enabled name must be loaded; skipped add-on explains why."""
    ensure_loaded()
    if name in _skipped_addons:
        raise BindError(
            f"{what} names {name!r} but that add-on failed to load: {_skipped_addons[name]}"
        )
    disabled = [
        item["name"]
        for item in _read_packaged()
        if item["type"] == kind and item["name"] == name and not item["enabled"]
    ]
    if disabled:
        raise BindError(f"{what} names disabled capability {name!r}.")


def impact_check(name: str, search_roots: list[Path] | None = None) -> list[Path]:
    """KPI YAML files that mention a capability name."""
    roots = search_roots or []
    if not roots:
        here = Path(__file__).resolve()
        udfs_kpis = here.parents[2] / "config" / "kpis"
        if udfs_kpis.is_dir():
            roots.append(udfs_kpis)
        tests = here.parents[3] / "tests"
        if tests.is_dir():
            roots.append(tests)
        roots.append(registries_dir())
    hits: list[Path] = []
    needle = str(name)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.yaml"):
            text = path.read_text()
            if needle in text:
                hits.append(path)
    return sorted(hits)


def generate_capabilities_markdown() -> str:
    """Human catalog generated from the four YAML registries."""
    rows = _read_packaged()
    lines = [
        "# Capability catalog",
        "",
        "Generated from `udfs/kpi_engine/registries/`. Do not hand-edit.",
        "",
        "This catalog covers column functions, measure functions, measure op kinds, and hooks.",
        "Filter operators, compose templates, time formats, and aggregations stay platform code.",
        "",
    ]
    groups = {
        "op": "Measure ops (`measures.op`)",
        "column_fn": "Column functions (`base_measures.op`)",
        "measure_fn": "Measure functions (`measures.fn`)",
        "hook": "Hooks (`measures.hook`)",
    }
    for kind, title in groups.items():
        items = [r for r in rows if r["type"] == kind]
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("_None registered._")
            lines.append("")
            continue
        for item in items:
            flag = "on" if item["enabled"] else "off"
            aliases = f" (aliases: {', '.join(item['aliases'])})" if item["aliases"] else ""
            lines.append(f"### `{item['name']}`{aliases}")
            lines.append("")
            lines.append(f"{item['description']}  ")
            lines.append(f"`role: {item['role']}` · `enabled: {flag}`")
            lines.append("")
            lines.append("```yaml")
            lines.append(item["example"].rstrip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def write_generated_docs(path: Path | None = None) -> Path:
    """Write the generated catalog next to the registry YAML files."""
    dest = path or (registries_dir() / "CAPABILITIES.md")
    dest.write_text(generate_capabilities_markdown())
    return dest
