"""Concatenate segregated context filters using a YAML template.

What this file provides
    parse_compose_template — validate `{code}` / `{code:02}` and literals.
    expand_compose — substitute context values and return consumed codes.
    strip_compose_keys — drop those keys so they are not unbound IN filters.

Where it is used
    binder (YAML). time_planner before claiming the period. bind_filters
    for non-time `filters:.compose`.

Capabilities
    Any literals between placeholders (`202607`, `2026/04`, `2026-04-15`).
    `{month:02}` zero-pads; `{month}` is literal concat (`7` stays `7`).

When to use
    Source has one period column; the host sent year / month / day separately.
"""

from __future__ import annotations

import re
from typing import Any

from kpi_engine.contracts import IncomingFilter
from kpi_engine.exceptions import BindError, TimePlanError
from kpi_engine.identifiers import norm_name

_PLACEHOLDER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?::0*(\d+))?$")


def parse_compose_template(template: str, *, what: str = "compose.template") -> tuple[tuple[str, int | None, str], ...]:
    """Split a template into (placeholder, pad) or ('', None, literal) tokens.

    Each token is (name, pad, literal). Placeholders have name set and literal ''.
    Literals have name '' and the literal text.
    """
    if not isinstance(template, str) or not template:
        raise BindError(f"{what} must not be empty.")
    tokens: list[tuple[str, int | None, str]] = []
    i = 0
    n = len(template)
    while i < n:
        if template[i] == "{":
            close = template.find("}", i + 1)
            if close < 0:
                raise BindError(f"{what} has an unmatched '{{'.")
            inner = template[i + 1 : close]
            if "{" in inner or "}" in inner:
                raise BindError(f"{what} cannot nest braces or call functions.")
            match = _PLACEHOLDER.match(inner.strip())
            if match is None:
                raise BindError(
                    f"{what} placeholder {{{inner}}} is not a filter code "
                    "(use {{year}}, {{month:02}}, …)."
                )
            pad = int(match.group(2)) if match.group(2) is not None else None
            tokens.append((match.group(1), pad, ""))
            i = close + 1
            continue
        start = i
        while i < n and template[i] != "{":
            i += 1
        tokens.append(("", None, template[start:i]))
    placeholders = [t for t in tokens if t[0]]
    if len(placeholders) < 2:
        raise BindError(f"{what} must name at least two context filters.")
    return tuple(tokens)


def compose_placeholder_names(template: str, *, what: str = "compose.template") -> tuple[str, ...]:
    """Context filter codes referenced by the template, in order, unique."""
    names: list[str] = []
    seen: set[str] = set()
    for name, _pad, _literal in parse_compose_template(template, what=what):
        if not name:
            continue
        key = norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return tuple(names)


def parse_compose_block(raw: Any, *, what: str) -> str | None:
    """Read compose: { template: '…' }. Absent/empty means no concat."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        template = raw.get("template")
        if template is None or (isinstance(template, str) and not template.strip()):
            raise BindError(f"{what}.template is required.")
        template = str(template)
    else:
        raise BindError(f"{what} must be an object with template:.")
    parse_compose_template(template, what=f"{what}.template")
    return template


def expand_compose(
    template: str,
    filters: tuple[IncomingFilter, ...],
    *,
    what: str = "compose.template",
) -> tuple[str, tuple[str, ...]]:
    """Substitute context values into the template. Each placeholder needs one value."""
    tokens = parse_compose_template(template, what=what)
    index = _filter_index(filters)
    chunks: list[str] = []
    consumed: list[str] = []
    seen: set[str] = set()
    for name, pad, literal in tokens:
        if not name:
            chunks.append(literal)
            continue
        item = index.get(norm_name(name))
        if item is None:
            raise TimePlanError(
                f"Compose placeholder {name!r} is missing from context."
            )
        if len(item.values) != 1 or item.values[0] is None:
            raise TimePlanError(
                f"Compose placeholder {name!r} must contain exactly one value "
                f"(got {len(item.values)})."
            )
        text = _stringify(item.values[0])
        if pad is not None:
            text = _zero_pad(text, pad)
        chunks.append(text)
        key = norm_name(item.code)
        if key not in seen:
            seen.add(key)
            consumed.append(item.code)
    return "".join(chunks), tuple(consumed)


def strip_compose_keys(
    filters: tuple[IncomingFilter, ...], template: str
) -> tuple[IncomingFilter, ...]:
    """Remove context keys named by the template so they are not leftover IN filters."""
    wanted = {norm_name(name) for name in compose_placeholder_names(template)}
    return tuple(
        item
        for item in filters
        if norm_name(item.code) not in wanted and norm_name(item.raw_key) not in wanted
    )


def _filter_index(filters: tuple[IncomingFilter, ...]) -> dict[str, IncomingFilter]:
    """Folded code / raw_key → first matching filter."""
    index: dict[str, IncomingFilter] = {}
    for item in filters:
        index.setdefault(norm_name(item.code), item)
        index.setdefault(norm_name(item.raw_key), item)
    return index


def _stringify(value: Any) -> str:
    """Context scalar as concat text (ints stay undotted)."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value).strip()


def _zero_pad(text: str, width: int) -> str:
    """Left-pad a digit string; non-digits are returned unchanged."""
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        sign = ""
        body = text
        if text.startswith("-"):
            sign = "-"
            body = text[1:]
        return sign + body.zfill(width)
    return text
