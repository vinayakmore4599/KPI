"""Safe identifiers and arithmetic expressions for KPI YAML.

What this file provides
    require_ident / quote_ident — DuckDB identifiers.
    parse_expression — AST for + - * / ( ) over column names and numbers.
    expression_columns — physical columns an expression names.
    compile_sql_expr — quoted rendering (tests / diagnostics only).

Where it is used
    binder validates YAML formulas. model_sql SELECTs expression_columns
    as physical columns. Pandas evaluates the AST after retrieve.

When to use
    KPI YAML formulas never go into DuckDB. quote_ident is for retrieve
    column lists, filters, and model SQL. Never concatenate filter values.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from collections.abc import Iterable
from typing import Union

from kpi_engine.exceptions import BindError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^(?:\d+\.\d*|\.\d+|\d+)$")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.\d*|\.\d+|\d+|[+\-*/()]")


@dataclass(frozen=True)
class Ident:
    """A column or measure name."""

    name: str


@dataclass(frozen=True)
class Number:
    """A numeric literal."""

    value: float
    text: str


@dataclass(frozen=True)
class Unary:
    """Unary + or -."""

    op: str
    operand: "Expr"


@dataclass(frozen=True)
class Binary:
    """Binary + - * /."""

    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Group:
    """Parentheses, preserved so SQL rendering stays byte-identical."""

    inner: "Expr"


Expr = Union[Ident, Number, Unary, Binary, Group]


def norm_name(value: str) -> str:
    """Fold case, spaces, and underscores so Region / region / reason code match."""
    return value.strip().lower().replace(" ", "_")


def match_name(name: str, columns: Iterable[str]) -> str | None:
    """Return the spelling already in `columns` whose folded name equals `name`."""
    wanted = norm_name(name)
    for col in columns:
        if norm_name(str(col)) == wanted:
            return str(col)
    return None


def require_ident(name: str, *, what: str = "identifier") -> str:
    """Accept only simple SQL names (letters, digits, underscore). Reject injection."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise BindError(f"Illegal {what}: {name!r}. Use a simple SQL identifier.")
    return name


def quote_ident(name: str) -> str:
    """Validate then wrap an identifier in double quotes for DuckDB."""
    return f'"{require_ident(name)}"'


def is_simple_ident(raw: str) -> bool:
    """True when the text is a single identifier with no operators."""
    return bool(isinstance(raw, str) and _IDENT.match(raw.strip()))


def parse_expression(raw: str, *, what: str = "measure sql") -> Expr:
    """Parse identifiers, numbers, + - * /, and parentheses into an AST."""
    return _parse_cached(raw, what)


def expression_columns(node: Expr) -> tuple[str, ...]:
    """Physical column / measure names the expression reads, left to right, unique."""
    seen: list[str] = []

    def walk(item: Expr) -> None:
        """Collect Ident names in source order."""
        if isinstance(item, Ident):
            if item.name not in seen:
                seen.append(item.name)
            return
        if isinstance(item, Number):
            return
        if isinstance(item, Unary):
            walk(item.operand)
            return
        if isinstance(item, Group):
            walk(item.inner)
            return
        walk(item.left)
        walk(item.right)

    walk(node)
    return tuple(seen)


def compile_sql_expr(raw: str, *, prefix: str = "", what: str = "measure sql") -> str:
    """Quoted rendering of an expression. DuckDB retrieve does not use this."""
    return _render_sql(parse_expression(raw, what=what), prefix)


@lru_cache(maxsize=256)
def _parse_cached(raw: str, what: str) -> Expr:
    """Cache parse by source text so bind / extract / eval share one AST."""
    if not isinstance(raw, str) or not raw.strip():
        raise BindError(f"Illegal {what}: {raw!r}. Use a column name or column * column.")
    text = raw.strip()
    if "--" in text or "/*" in text or "*/" in text:
        raise BindError(
            f"Illegal {what}: {raw!r}. Only column names, numbers, and + - * / ( ) are allowed."
        )
    tokens = _TOKEN.findall(text)
    if "".join(tokens) != re.sub(r"\s+", "", text):
        raise BindError(
            f"Illegal {what}: {raw!r}. Only column names, numbers, and + - * / ( ) are allowed."
        )
    parser = _Parser(tokens, raw, what)
    node = parser.parse_expr()
    if parser.i != len(tokens):
        raise BindError(
            f"Illegal {what}: {raw!r}. Only column names, numbers, and + - * / ( ) are allowed."
        )
    if not expression_columns(node):
        raise BindError(f"Illegal {what}: {raw!r}. Expression must name at least one column.")
    return node


class _Parser:
    """Recursive descent: expr → term → factor."""

    def __init__(self, tokens: list[str], raw: str, what: str) -> None:
        self.tokens = tokens
        self.raw = raw
        self.what = what
        self.i = 0

    def parse_expr(self) -> Expr:
        """Addition and subtraction."""
        node = self.parse_term()
        while self._peek() in {"+", "-"}:
            op = self._next()
            node = Binary(op, node, self.parse_term())
        return node

    def parse_term(self) -> Expr:
        """Multiplication and division."""
        node = self.parse_factor()
        while self._peek() in {"*", "/"}:
            op = self._next()
            node = Binary(op, node, self.parse_factor())
        return node

    def parse_factor(self) -> Expr:
        """Unary, group, identifier, or number."""
        tok = self._peek()
        if tok is None:
            raise BindError(f"Illegal {self.what}: {self.raw!r}. Incomplete or unbalanced expression.")
        if tok in {"+", "-"}:
            op = self._next()
            return Unary(op, self.parse_factor())
        if tok == "(":
            self._next()
            inner = self.parse_expr()
            if self._peek() != ")":
                raise BindError(f"Illegal {self.what}: {self.raw!r}. Unbalanced parentheses.")
            self._next()
            return Group(inner)
        if _IDENT.match(tok):
            name = self._next()
            if self._peek() == "(":
                raise BindError(
                    f"Illegal {self.what}: {self.raw!r}. Function calls are not allowed; "
                    "put the expression in sql: and the aggregation in agg:."
                )
            return Ident(name)
        if _NUMBER.match(tok):
            text = self._next()
            return Number(float(text), text)
        raise BindError(
            f"Illegal {self.what}: {self.raw!r}. Only column names, numbers, and + - * / ( ) are allowed."
        )

    def _peek(self) -> str | None:
        """Current token, or None at end."""
        if self.i >= len(self.tokens):
            return None
        return self.tokens[self.i]

    def _next(self) -> str:
        """Consume and return the current token."""
        tok = self._peek()
        if tok is None:
            raise BindError(f"Illegal {self.what}: {self.raw!r}. Incomplete or unbalanced expression.")
        self.i += 1
        return tok


def _render_sql(node: Expr, prefix: str) -> str:
    """Space-joined quoted SQL matching the previous compiler's output."""
    if isinstance(node, Ident):
        return f"{prefix}{quote_ident(node.name)}"
    if isinstance(node, Number):
        return node.text
    if isinstance(node, Unary):
        return f"{node.op} {_render_sql(node.operand, prefix)}"
    if isinstance(node, Group):
        return f"( {_render_sql(node.inner, prefix)} )"
    return f"{_render_sql(node.left, prefix)} {node.op} {_render_sql(node.right, prefix)}"
