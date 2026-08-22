"""Safe identifiers and formulas for KPI YAML.

What this file provides
    require_ident / quote_ident — DuckDB identifiers.
    parse_expression — AST for arithmetic, CASE, comparisons, and allowlisted calls.
    expression_columns — physical columns / measures an expression names.
    compile_sql_expr — quoted rendering (tests / diagnostics only).

Where it is used
    binder validates YAML formulas. model_sql SELECTs expression_columns
    as physical columns. Pandas evaluates the AST after retrieve.

When to use
    KPI YAML formulas never go into DuckDB. quote_ident is for retrieve
    column lists, filters, and model SQL. Never concatenate filter values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Union

from kpi_engine.exceptions import BindError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^(?:\d+\.\d*|\.\d+|\d+)$")
_KEYWORDS = frozenset(
    {"case", "when", "then", "else", "end", "and", "or", "not", "is", "null"}
)
_COMPARE_OPS = frozenset({"=", "<>", "!=", "<", ">", "<=", ">="})


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
class String:
    """A single-quoted string literal."""

    value: str


@dataclass(frozen=True)
class Null:
    """SQL NULL."""


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
class Compare:
    """= <> != < > <= >=."""

    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class BoolOp:
    """AND / OR."""

    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Not:
    """NOT cond."""

    operand: "Expr"


@dataclass(frozen=True)
class IsNull:
    """IS NULL / IS NOT NULL."""

    operand: "Expr"
    invert: bool = False


@dataclass(frozen=True)
class Call:
    """Allowlisted function call."""

    name: str
    args: tuple["Expr", ...]


@dataclass(frozen=True)
class Case:
    """CASE WHEN … THEN … [ELSE …] END."""

    whens: tuple[tuple["Expr", "Expr"], ...]
    else_: "Expr | None" = None


@dataclass(frozen=True)
class Group:
    """Parentheses, preserved so SQL rendering stays byte-identical."""

    inner: "Expr"


Expr = Union[
    Ident,
    Number,
    String,
    Null,
    Unary,
    Binary,
    Compare,
    BoolOp,
    Not,
    IsNull,
    Call,
    Case,
    Group,
]


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
    """Parse a formula into an AST."""
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
        if isinstance(item, (Number, String, Null)):
            return
        if isinstance(item, (Unary, Not, Group)):
            walk(item.operand if not isinstance(item, Group) else item.inner)
            return
        if isinstance(item, IsNull):
            walk(item.operand)
            return
        if isinstance(item, Call):
            for arg in item.args:
                walk(arg)
            return
        if isinstance(item, Case):
            for cond, value in item.whens:
                walk(cond)
                walk(value)
            if item.else_ is not None:
                walk(item.else_)
            return
        walk(item.left)
        walk(item.right)

    walk(node)
    return tuple(seen)


def expr_call_names(node: Expr) -> tuple[str, ...]:
    """Function names used in calls, left to right."""
    names: list[str] = []

    def walk(item: Expr) -> None:
        """Collect Call names."""
        if isinstance(item, Call):
            names.append(item.name)
            for arg in item.args:
                walk(arg)
            return
        if isinstance(item, (Number, String, Null, Ident)):
            return
        if isinstance(item, (Unary, Not, IsNull)):
            walk(item.operand)
            return
        if isinstance(item, Group):
            walk(item.inner)
            return
        if isinstance(item, Case):
            for cond, value in item.whens:
                walk(cond)
                walk(value)
            if item.else_ is not None:
                walk(item.else_)
            return
        walk(item.left)
        walk(item.right)

    walk(node)
    return tuple(names)


def assert_expr_calls(node: Expr, allowed: Mapping[str, object], *, what: str) -> None:
    """Bind-error when a call is not in the layer's function registry."""
    keys = {str(name).lower(): str(name) for name in allowed}
    for name in expr_call_names(node):
        if name.lower() not in keys:
            raise BindError(
                f"{what} names unknown function {name!r}. Registered: {sorted(allowed)}."
            )


def compile_sql_expr(raw: str, *, prefix: str = "", what: str = "measure sql") -> str:
    """Quoted rendering of an expression. DuckDB retrieve does not use this."""
    return _render_sql(parse_expression(raw, what=what), prefix)


def _illegal(raw: str, what: str, extra: str = "") -> BindError:
    """Shared illegal-formula error."""
    hint = extra or (
        "Only column names, numbers, + - * /, CASE, comparisons, "
        "IS NULL, AND/OR/NOT, and allowlisted function calls are allowed."
    )
    return BindError(f"Illegal {what}: {raw!r}. {hint}")


@lru_cache(maxsize=256)
def _parse_cached(raw: str, what: str) -> Expr:
    """Cache parse by source text so bind / extract / eval share one AST."""
    if not isinstance(raw, str) or not raw.strip():
        raise BindError(f"Illegal {what}: {raw!r}. Use a column name or column * column.")
    text = raw.strip()
    if "--" in text or "/*" in text or "*/" in text or ";" in text or '"' in text:
        raise _illegal(raw, what)
    tokens = _tokenize(text, raw, what)
    parser = _Parser(tokens, raw, what)
    node = parser.parse_or()
    if parser.i != len(tokens):
        raise _illegal(raw, what)
    if not expression_columns(node):
        raise BindError(f"Illegal {what}: {raw!r}. Expression must name at least one column.")
    return node


def _tokenize(text: str, raw: str, what: str) -> list[str]:
    """Scan identifiers, numbers, strings, and operators. Whitespace is a separator."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            i += 1
            buf: list[str] = []
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    tokens.append("'" + "".join(buf) + "'")
                    break
                buf.append(text[i])
                i += 1
            else:
                raise BindError(f"Illegal {what}: {raw!r}. Unclosed string literal.")
            continue
        if text.startswith("<>", i) or text.startswith("!=", i) or text.startswith(
            "<=", i
        ) or text.startswith(">=", i):
            tokens.append(text[i : i + 2])
            i += 2
            continue
        if ch in "+-*/()=<>,":
            tokens.append(ch)
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            saw_dot = ch == "."
            while j < n and (text[j].isdigit() or (text[j] == "." and not saw_dot)):
                if text[j] == ".":
                    saw_dot = True
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        raise _illegal(raw, what)
    return tokens


class _Parser:
    """Recursive descent: or → and → not → compare → add → mul → factor."""

    def __init__(self, tokens: list[str], raw: str, what: str) -> None:
        self.tokens = tokens
        self.raw = raw
        self.what = what
        self.i = 0

    def parse_or(self) -> Expr:
        """OR."""
        node = self.parse_and()
        while self._is_kw("or"):
            self._next()
            node = BoolOp("or", node, self.parse_and())
        return node

    def parse_and(self) -> Expr:
        """AND."""
        node = self.parse_not()
        while self._is_kw("and"):
            self._next()
            node = BoolOp("and", node, self.parse_not())
        return node

    def parse_not(self) -> Expr:
        """NOT."""
        if self._is_kw("not"):
            self._next()
            return Not(self.parse_not())
        return self.parse_compare()

    def parse_compare(self) -> Expr:
        """Comparisons and IS [NOT] NULL."""
        node = self.parse_add()
        if self._is_kw("is"):
            self._next()
            invert = False
            if self._is_kw("not"):
                self._next()
                invert = True
            if not self._is_kw("null"):
                raise _illegal(self.raw, self.what, "Expected NULL after IS.")
            self._next()
            return IsNull(node, invert)
        op = self._peek()
        if op in _COMPARE_OPS:
            self._next()
            return Compare(op, node, self.parse_add())
        return node

    def parse_add(self) -> Expr:
        """Addition and subtraction."""
        node = self.parse_mul()
        while self._peek() in {"+", "-"}:
            op = self._next()
            node = Binary(op, node, self.parse_mul())
        return node

    def parse_mul(self) -> Expr:
        """Multiplication and division."""
        node = self.parse_factor()
        while self._peek() in {"*", "/"}:
            op = self._next()
            node = Binary(op, node, self.parse_factor())
        return node

    def parse_factor(self) -> Expr:
        """Unary, CASE, call, group, identifier, number, string, or NULL."""
        tok = self._peek()
        if tok is None:
            raise BindError(
                f"Illegal {self.what}: {self.raw!r}. Incomplete or unbalanced expression."
            )
        if tok in {"+", "-"}:
            op = self._next()
            return Unary(op, self.parse_factor())
        if self._is_kw("case"):
            return self._parse_case()
        if self._is_kw("null"):
            self._next()
            return Null()
        if tok == "(":
            self._next()
            inner = self.parse_or()
            if self._peek() != ")":
                raise BindError(f"Illegal {self.what}: {self.raw!r}. Unbalanced parentheses.")
            self._next()
            return Group(inner)
        if tok.startswith("'") and tok.endswith("'") and len(tok) >= 2:
            self._next()
            return String(tok[1:-1].replace("''", "'"))
        if _IDENT.match(tok):
            name = self._next()
            if self._peek() == "(":
                return self._parse_call(name)
            if name.lower() in _KEYWORDS:
                raise _illegal(
                    self.raw,
                    self.what,
                    f"{name} is reserved in formulas.",
                )
            return Ident(name)
        if _NUMBER.match(tok):
            text = self._next()
            return Number(float(text), text)
        raise _illegal(self.raw, self.what)

    def _parse_call(self, name: str) -> Call:
        """name(arg, arg, …)."""
        self._next()
        args: list[Expr] = []
        if self._peek() != ")":
            args.append(self.parse_or())
            while self._peek() == ",":
                self._next()
                args.append(self.parse_or())
        if self._peek() != ")":
            raise BindError(f"Illegal {self.what}: {self.raw!r}. Unbalanced parentheses.")
        self._next()
        return Call(name, tuple(args))

    def _parse_case(self) -> Case:
        """CASE WHEN cond THEN value [WHEN …] [ELSE value] END."""
        self._next()
        whens: list[tuple[Expr, Expr]] = []
        while self._is_kw("when"):
            self._next()
            cond = self.parse_or()
            if not self._is_kw("then"):
                raise _illegal(self.raw, self.what, "Expected THEN after WHEN.")
            self._next()
            value = self.parse_or()
            whens.append((cond, value))
        if not whens:
            raise _illegal(self.raw, self.what, "CASE needs at least one WHEN.")
        else_: Expr | None = None
        if self._is_kw("else"):
            self._next()
            else_ = self.parse_or()
        if not self._is_kw("end"):
            raise _illegal(self.raw, self.what, "CASE needs END.")
        self._next()
        return Case(tuple(whens), else_)

    def _is_kw(self, word: str) -> bool:
        """True when the current token is this keyword (any case)."""
        tok = self._peek()
        return bool(tok and tok.lower() == word and _IDENT.match(tok))

    def _peek(self) -> str | None:
        """Current token, or None at end."""
        if self.i >= len(self.tokens):
            return None
        return self.tokens[self.i]

    def _next(self) -> str:
        """Consume and return the current token."""
        tok = self._peek()
        if tok is None:
            raise BindError(
                f"Illegal {self.what}: {self.raw!r}. Incomplete or unbalanced expression."
            )
        self.i += 1
        return tok


def _render_sql(node: Expr, prefix: str) -> str:
    """Space-joined quoted SQL matching the previous compiler's output for arithmetic."""
    if isinstance(node, Ident):
        return f"{prefix}{quote_ident(node.name)}"
    if isinstance(node, Number):
        return node.text
    if isinstance(node, String):
        escaped = node.value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(node, Null):
        return "NULL"
    if isinstance(node, Unary):
        return f"{node.op} {_render_sql(node.operand, prefix)}"
    if isinstance(node, Not):
        return f"NOT {_render_sql(node.operand, prefix)}"
    if isinstance(node, IsNull):
        mid = "IS NOT NULL" if node.invert else "IS NULL"
        return f"{_render_sql(node.operand, prefix)} {mid}"
    if isinstance(node, Group):
        return f"( {_render_sql(node.inner, prefix)} )"
    if isinstance(node, Call):
        inner = ", ".join(_render_sql(arg, prefix) for arg in node.args)
        return f"{node.name}({inner})"
    if isinstance(node, Case):
        parts = ["CASE"]
        for cond, value in node.whens:
            parts.append(f"WHEN {_render_sql(cond, prefix)} THEN {_render_sql(value, prefix)}")
        if node.else_ is not None:
            parts.append(f"ELSE {_render_sql(node.else_, prefix)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(node, Compare):
        return f"{_render_sql(node.left, prefix)} {node.op} {_render_sql(node.right, prefix)}"
    if isinstance(node, BoolOp):
        return f"{_render_sql(node.left, prefix)} {node.op.upper()} {_render_sql(node.right, prefix)}"
    return f"{_render_sql(node.left, prefix)} {node.op} {_render_sql(node.right, prefix)}"
