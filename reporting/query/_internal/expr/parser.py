"""Parse expression text into the typed AST, enforcing size limits."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from importlib import resources

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import LarkError, UnexpectedCharacters, UnexpectedEOF, UnexpectedInput

from reporting.query._internal.expr.ast import (
    BinaryOp,
    CalcRef,
    Case,
    FieldRef,
    FuncCall,
    Literal,
    Node,
    UnaryOp,
    depth,
    walk,
)

MAX_CHARS = 500
MAX_NODES = 100
MAX_DEPTH = 10

_GRAMMAR = resources.files(__package__).joinpath("grammar.lark").read_text()
_PARSER = Lark(_GRAMMAR, parser="lalr", maybe_placeholders=True)


class ExprError(Exception):
    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.position = position


@v_args(inline=True)
class _ToAst(Transformer[Token, Node]):
    def number(self, tok: Token) -> Literal:
        text = str(tok)
        if any(c in text for c in ".eE"):
            try:
                return Literal(Decimal(text))
            except InvalidOperation:
                raise ExprError(f"Invalid number {text}") from None
        return Literal(int(text))

    def string(self, tok: Token) -> Literal:
        return Literal(str(tok)[1:-1].replace("''", "'"))

    def true(self) -> Literal:
        return Literal(True)

    def false(self) -> Literal:
        return Literal(False)

    def null(self) -> Literal:
        return Literal(None)

    def field_ref(self, alias: Token, column: Token) -> FieldRef:
        col = str(column)
        if col.startswith('"'):
            col = col[1:-1].replace('""', '"')
        return FieldRef(str(alias), col)

    def calc_ref(self, tok: Token) -> CalcRef:
        return CalcRef(str(tok)[1:-1])

    def func_call(self, name: Token, *args: Node | None) -> FuncCall:
        return FuncCall(str(name).upper(), tuple(a for a in args if a is not None))

    def when_clause(self, cond: Node, result: Node) -> tuple[Node, Node]:
        return (cond, result)

    def case_expr(self, *parts: object) -> Case:
        *whens, else_ = parts
        return Case(tuple(whens), else_)  # type: ignore[arg-type]

    def binop(self, left: Node, op: Token, right: Node) -> BinaryOp:
        return BinaryOp(str(op), left, right)

    def compare(self, left: Node, op: Token, right: Node) -> BinaryOp:
        return BinaryOp(str(op), left, right)

    def and_(self, left: Node, right: Node) -> BinaryOp:
        return BinaryOp("AND", left, right)

    def or_(self, left: Node, right: Node) -> BinaryOp:
        return BinaryOp("OR", left, right)

    def not_(self, operand: Node) -> UnaryOp:
        return UnaryOp("NOT", operand)

    def neg(self, operand: Node) -> Node:
        if isinstance(operand, Literal) and isinstance(operand.value, (int, Decimal)) \
                and not isinstance(operand.value, bool):
            return Literal(-operand.value)
        return UnaryOp("NEG", operand)


def parse(text: str) -> Node:
    if len(text) > MAX_CHARS:
        raise ExprError(f"Expression is longer than {MAX_CHARS} characters")
    if not text.strip():
        raise ExprError("Expression is empty")
    try:
        tree = _PARSER.parse(text)
        node: Node = _ToAst().transform(tree)
    except ExprError:
        raise
    except UnexpectedCharacters as exc:
        pos = exc.pos_in_stream or 0
        raise ExprError(f"Unexpected character {text[pos]!r} at position {exc.column}",
                        pos) from None
    except UnexpectedEOF:
        raise ExprError("Expression ends unexpectedly", len(text)) from None
    except UnexpectedInput as exc:
        tok = getattr(exc, "token", None)
        if tok is not None and getattr(tok, "type", "") == "$END":
            raise ExprError("Expression ends unexpectedly", len(text)) from None
        where = getattr(exc, "pos_in_stream", None)
        shown = f"'{tok}'" if tok is not None and str(tok) else "end of input"
        raise ExprError(f"Unexpected {shown} at position {getattr(exc, 'column', '?')}",
                        where) from None
    except LarkError as exc:  # pragma: no cover - defensive
        cause = exc.__context__ if isinstance(exc.__context__, ExprError) else None
        if cause is not None:
            raise cause from None
        raise ExprError("Expression could not be parsed") from None
    if len(walk(node)) > MAX_NODES:
        raise ExprError(f"Expression has more than {MAX_NODES} parts")
    if depth(node) > MAX_DEPTH:
        raise ExprError(f"Expression is nested more than {MAX_DEPTH} levels deep")
    return node
