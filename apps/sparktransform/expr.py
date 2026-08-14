"""A whitelisted, non-Turing-complete expression grammar for a Spark-mode
`SparkTransformConfig` column's `transform_expr` -- see
docs/decisions/0009-spark-backed-transform-mode.md.

This is deliberately small and closed, the same posture
`apps.authproviders.token_support`'s placeholder substitution already
takes: a hand-written recursive-descent parser producing a typed AST,
never `eval`/`exec`, never a general expression language. An expression
that doesn't parse against this grammar is a `TransformExpressionError`
(raised at `SparkTransformConfig` save time -- see
`apps.sparktransform.services.validate_spark_transform_config` --
never a runtime surprise inside a running Spark job).

Grammar (EBNF-ish):

    expr    := term (("+" | "-") term)*
    term    := factor (("*" | "/") factor)*
    factor  := NUMBER
             | "-" factor
             | "CAST" "(" expr "AS" TYPE ")"
             | FUNC "(" expr ")"
             | FIELD
             | "(" expr ")"

    FUNC := "TRIM" | "UPPER" | "LOWER"           (case-insensitive)
    TYPE := "INTEGER" | "DECIMAL" | "TEXT" | "DATE" | "BOOLEAN"  (case-insensitive)
    FIELD := a dotted path, e.g. `policy.premium` -- same dotted-path
             convention as apps.core.paths.get_by_path, and the same
             character set: letters, digits, underscore, dot.

Two things consume the parsed AST:

  - `evaluate_transform_expr(expr, record)` -- a plain-Python reference
    evaluator (dict lookups via apps.core.paths.get_by_path), useful for
    testing the grammar's semantics without a real Spark cluster, and as
    the definition of what "correct" means for any execution engine.
  - `compile_to_spark_sql(node, field_aliases)` -- translates the AST
    into a Spark SQL expression string built entirely from whitelisted
    pieces (backtick-quoted deterministic aliases, whitelisted function/
    type names, the four arithmetic operators, and numeric literals this
    module itself tokenized) -- never by interpolating a source field's
    *name* into SQL text. `apps.sparktransform.services` uses this to
    pre-compile every column's expression once, at job-submission time,
    into the payload a submitted Spark job trusts and just runs -- the
    job itself never re-parses tenant-supplied `transform_expr` text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.core.exceptions import TransformExpressionError

MAX_EXPR_LENGTH = 500

_ALLOWED_FUNCS = frozenset({"TRIM", "UPPER", "LOWER"})
_ALLOWED_CAST_TYPES = frozenset({"INTEGER", "DECIMAL", "TEXT", "DATE", "BOOLEAN"})
_SPARK_SQL_CAST_TYPE = {
    "INTEGER": "INT",
    "DECIMAL": "DECIMAL(18,4)",
    "TEXT": "STRING",
    "DATE": "DATE",
    "BOOLEAN": "BOOLEAN",
}

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_TOKEN_RE = re.compile(
    r"""
    (?P<NUMBER>\d+(\.\d+)?)
  | (?P<FIELD>[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<OP>[+\-*/])
  | (?P<WS>\s+)
    """,
    re.VERBOSE,
)


# --- AST ------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRef:
    name: str


@dataclass(frozen=True)
class NumberLiteral:
    value: float


@dataclass(frozen=True)
class FuncCall:
    name: str
    arg: Node


@dataclass(frozen=True)
class Cast:
    expr: Node
    target_type: str


@dataclass(frozen=True)
class BinOp:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Negate:
    expr: Node


Node = FieldRef | NumberLiteral | FuncCall | Cast | BinOp | Negate


# --- Tokenizer --------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


def _tokenize(expr: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if match is None:
            raise TransformExpressionError(
                f"transform_expr {expr!r}: unrecognized character at position {pos}"
            )
        pos = match.end()
        kind = match.lastgroup
        if kind == "WS":
            continue
        tokens.append(_Token(kind=kind, text=match.group()))
    return tokens


# --- Parser -----------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: list[_Token], *, original_expr: str):
        self._tokens = tokens
        self._pos = 0
        self._original_expr = original_expr

    def _peek(self) -> _Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> _Token:
        token = self._peek()
        if token is None:
            raise TransformExpressionError(
                f"transform_expr {self._original_expr!r}: unexpected end of expression"
            )
        self._pos += 1
        return token

    def _expect(self, kind: str, text: str | None = None) -> _Token:
        token = self._advance()
        if token.kind != kind or (text is not None and token.text.upper() != text):
            raise TransformExpressionError(
                f"transform_expr {self._original_expr!r}: expected {text or kind}, "
                f"got {token.text!r}"
            )
        return token

    def parse(self) -> Node:
        node = self._expr()
        if self._peek() is not None:
            raise TransformExpressionError(
                f"transform_expr {self._original_expr!r}: unexpected trailing "
                f"{self._peek().text!r}"
            )
        return node

    def _expr(self) -> Node:
        node = self._term()
        while (token := self._peek()) is not None and token.kind == "OP" and token.text in "+-":
            op = self._advance().text
            node = BinOp(op=op, left=node, right=self._term())
        return node

    def _term(self) -> Node:
        node = self._factor()
        while (token := self._peek()) is not None and token.kind == "OP" and token.text in "*/":
            op = self._advance().text
            node = BinOp(op=op, left=node, right=self._factor())
        return node

    def _factor(self) -> Node:
        token = self._peek()
        if token is None:
            raise TransformExpressionError(
                f"transform_expr {self._original_expr!r}: unexpected end of expression"
            )

        if token.kind == "OP" and token.text == "-":
            self._advance()
            return Negate(expr=self._factor())

        if token.kind == "NUMBER":
            self._advance()
            return NumberLiteral(value=float(token.text))

        if token.kind == "LPAREN":
            self._advance()
            node = self._expr()
            self._expect("RPAREN")
            return node

        if token.kind == "FIELD":
            upper = token.text.upper()
            if upper == "CAST":
                return self._cast()
            if upper in _ALLOWED_FUNCS:
                return self._func_call(upper)
            self._advance()
            return FieldRef(name=token.text)

        raise TransformExpressionError(
            f"transform_expr {self._original_expr!r}: unexpected token {token.text!r}"
        )

    def _func_call(self, name: str) -> FuncCall:
        self._advance()  # the function name itself
        self._expect("LPAREN")
        arg = self._expr()
        self._expect("RPAREN")
        return FuncCall(name=name, arg=arg)

    def _cast(self) -> Cast:
        self._advance()  # CAST
        self._expect("LPAREN")
        expr = self._expr()
        self._expect("FIELD", "AS")
        type_token = self._expect("FIELD")
        target_type = type_token.text.upper()
        if target_type not in _ALLOWED_CAST_TYPES:
            raise TransformExpressionError(
                f"transform_expr {self._original_expr!r}: unsupported CAST target type "
                f"{type_token.text!r}; allowed: {sorted(_ALLOWED_CAST_TYPES)}"
            )
        self._expect("RPAREN")
        return Cast(expr=expr, target_type=target_type)


def parse_transform_expr(expr: str) -> Node:
    """Parse `expr` against the whitelisted grammar, or raise
    `TransformExpressionError`. Never `eval`/`exec`."""
    if not isinstance(expr, str) or not expr.strip():
        raise TransformExpressionError("transform_expr must be a non-empty string")
    if len(expr) > MAX_EXPR_LENGTH:
        raise TransformExpressionError(
            f"transform_expr exceeds the maximum allowed length ({MAX_EXPR_LENGTH} characters)"
        )
    tokens = _tokenize(expr)
    if not tokens:
        raise TransformExpressionError("transform_expr must be a non-empty string")
    return _Parser(tokens, original_expr=expr).parse()


def validate_transform_expr(expr: str) -> None:
    """Raise `TransformExpressionError` if `expr` doesn't parse. Callers
    that don't need the AST itself should prefer this over
    `parse_transform_expr` for clarity at the call site."""
    parse_transform_expr(expr)


def validate_source_field(source_field: str) -> None:
    """A `source_field` (the dotted path a column reads from) is walked
    with `apps.core.paths.get_by_path`, not this grammar's parser -- but
    it still must not contain anything a `get_json_object` JSONPath
    lookup (see `spark_jobs/transform_job.py`) could interpret specially
    (`*`, `[`, `]`, quotes, ...). Same conservative character set as a
    grammar FIELD token."""
    if not isinstance(source_field, str) or not _FIELD_RE.match(source_field):
        raise TransformExpressionError(
            f"source_field {source_field!r} must be a dotted path of letters, digits, "
            "and underscores (e.g. 'policy.premium')"
        )


# --- Reference evaluator (dict-based; no Spark required) -------------------


def evaluate_transform_expr(expr: str, record: dict) -> object:
    """Evaluate `expr` against a plain `record` dict, using
    `apps.core.paths.get_by_path` for field lookups -- the same dotted-path
    semantics `apps.pipelines.mapping` uses. This is the reference
    implementation of the grammar's semantics: a Spark job execution of
    the same `transform_expr` (via `compile_to_spark_sql`) must agree with
    this on every well-formed input. Useful for testing the grammar
    without a real Spark cluster; not on any production hot path (Spark
    itself performs the actual per-record evaluation for Spark-mode
    pipelines)."""
    return _evaluate(parse_transform_expr(expr), record)


def _evaluate(node: Node, record: dict) -> object:
    from apps.core.paths import get_by_path

    if isinstance(node, NumberLiteral):
        return node.value
    if isinstance(node, FieldRef):
        value, _found = get_by_path(record, node.name)
        return value
    if isinstance(node, Negate):
        return -_evaluate(node.expr, record)
    if isinstance(node, FuncCall):
        value = _evaluate(node.arg, record)
        text = "" if value is None else str(value)
        if node.name == "TRIM":
            return text.strip()
        if node.name == "UPPER":
            return text.upper()
        if node.name == "LOWER":
            return text.lower()
        raise AssertionError(f"unreachable: unknown whitelisted function {node.name!r}")
    if isinstance(node, Cast):
        value = _evaluate(node.expr, record)
        return _cast_value(value, node.target_type)
    if isinstance(node, BinOp):
        left = _evaluate(node.left, record)
        right = _evaluate(node.right, record)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            return left / right
        raise AssertionError(f"unreachable: unknown whitelisted operator {node.op!r}")
    raise AssertionError(f"unreachable: unknown AST node {node!r}")


def _cast_value(value: object, target_type: str) -> object:
    if value is None:
        return None
    if target_type == "INTEGER":
        return int(float(value))
    if target_type == "DECIMAL":
        return float(value)
    if target_type == "TEXT":
        return str(value)
    if target_type == "BOOLEAN":
        return bool(value)
    if target_type == "DATE":
        return str(value)
    raise AssertionError(f"unreachable: unknown whitelisted cast type {target_type!r}")


# --- Spark SQL compiler ------------------------------------------------


def compile_to_spark_sql(node: Node, field_aliases: dict[str, str]) -> str:
    """Translate a parsed `Node` into a Spark SQL expression string built
    entirely from whitelisted pieces: backtick-quoted deterministic
    column aliases from `field_aliases` (never a tenant-supplied string
    interpolated directly), the whitelisted function/CAST-type names this
    grammar already restricts to, the four arithmetic operators, and
    numeric literals this module itself tokenized as digits. There is no
    string interpolation of anything tenant-controlled into SQL text.

    `field_aliases` maps each `FieldRef.name` appearing in the expression
    to the safe column alias (see `apps.sparktransform.services`) the
    submitted Spark job will have already extracted that field into,
    e.g. `{"policy.premium": "__src_0"}`. Every `FieldRef` in `node` must
    have an entry -- callers build this by parsing all of a
    `SparkTransformConfig`'s columns together, not one column at a time.
    """
    if isinstance(node, NumberLiteral):
        return repr(node.value)
    if isinstance(node, FieldRef):
        alias = field_aliases.get(node.name)
        if alias is None:
            raise TransformExpressionError(
                f"no compiled alias for field {node.name!r} -- internal error, "
                "compile_to_spark_sql must be called with a complete field_aliases map"
            )
        return f"`{alias}`"
    if isinstance(node, Negate):
        return f"(-{compile_to_spark_sql(node.expr, field_aliases)})"
    if isinstance(node, FuncCall):
        return f"{node.name}({compile_to_spark_sql(node.arg, field_aliases)})"
    if isinstance(node, Cast):
        spark_type = _SPARK_SQL_CAST_TYPE[node.target_type]
        return f"CAST({compile_to_spark_sql(node.expr, field_aliases)} AS {spark_type})"
    if isinstance(node, BinOp):
        left = compile_to_spark_sql(node.left, field_aliases)
        right = compile_to_spark_sql(node.right, field_aliases)
        return f"({left} {node.op} {right})"
    raise AssertionError(f"unreachable: unknown AST node {node!r}")
