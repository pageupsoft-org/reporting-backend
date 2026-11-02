"""Compile a type-checked AST node by node into SQLAlchemy constructs.

Identifiers are looked up through callbacks (never copied from the text) and every literal
becomes a bound parameter, so there is no path for expression text to reach the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    Integer,
    Numeric,
    String,
    and_,
    case,
    cast,
    distinct,
    extract,
    func,
    not_,
    null,
    or_,
)
from sqlalchemy.sql.elements import ColumnElement

from reporting.core import DataType, Dialect
from reporting.query._internal.constructs import DECIMAL_38_6, Ceiling, CharLength, DateDiff, Floor
from reporting.query._internal.expr.ast import (
    BinaryOp,
    CalcRef,
    Case,
    FieldRef,
    FuncCall,
    Literal,
    Node,
    UnaryOp,
)
from reporting.query._internal.expr.checker import CheckResult

Expr = ColumnElement[Any]


class Binder:
    """Hands out sequential bind-parameter names (p1, p2, ...) for one statement."""

    def __init__(self) -> None:
        self._n = 0

    def bind(self, value: Any, type_: Any = None, expanding: bool = False) -> Expr:
        from sqlalchemy import bindparam

        self._n += 1
        return bindparam(f"p{self._n}", value=value, type_=type_, expanding=expanding)


def sa_type(dtype: DataType | str) -> Any:
    types: dict[DataType | str, Any] = {
        DataType.INTEGER: Integer(), DataType.DECIMAL: Numeric(), DataType.FLOAT: Float(),
        DataType.BOOLEAN: Boolean(), DataType.TEXT: String(),
    }
    return types.get(dtype)


def true_divide(left: Expr, right: Expr, left_type: DataType | str,
                right_type: DataType | str, dialect: Dialect) -> Expr:
    """Division that never truncates, on every engine."""
    if dialect is Dialect.SQLITE:
        # SQLite stores NUMERIC values with integral value as integers, so cast to REAL.
        return cast(left, Float()) / right
    if left_type == DataType.INTEGER and right_type == DataType.INTEGER:
        return cast(left, Numeric(38, 10)) / right
    return left / right


def aggregate(fn: str, arg: Expr | None, arg_type: DataType | str | None,
              dialect: Dialect) -> Expr:
    """The aggregate functions of plan §4.1 with their dialect notes applied."""
    fn = fn.lower()
    if arg is None:
        return func.count()
    if fn == "avg":
        if dialect is Dialect.MSSQL and arg_type == DataType.INTEGER:
            arg = cast(arg, DECIMAL_38_6)  # AVG(int) returns int on SQL Server
        return func.avg(arg)
    if fn == "sum":
        if dialect is Dialect.MSSQL and arg_type == DataType.INTEGER:
            from sqlalchemy import BigInteger

            arg = cast(arg, BigInteger())  # widen to avoid INT overflow
        return func.sum(arg)
    if fn == "count":
        return func.count(arg)
    if fn == "count_distinct":
        return func.count(distinct(arg))
    if fn == "min":
        return func.min(arg)
    if fn == "max":
        return func.max(arg)
    raise ValueError(fn)


class ExprCompiler:
    def __init__(self, checked: CheckResult, dialect: Dialect, binder: Binder,
                 field: Callable[[str, str], Expr], calc: Callable[[str], Expr]) -> None:
        self._checked = checked
        self._dialect = dialect
        self._binder = binder
        self._field = field
        self._calc = calc

    def _type(self, node: Node) -> DataType | str:
        return self._checked.types.get(id(node), DataType.OTHER)

    def compile(self, node: Node) -> Expr:
        if isinstance(node, Literal):
            return self._literal(node)
        if isinstance(node, FieldRef):
            return self._field(node.alias, node.column)
        if isinstance(node, CalcRef):
            return self._calc(node.name)
        if isinstance(node, UnaryOp):
            inner = self.compile(node.operand)
            return not_(inner) if node.op == "NOT" else -inner
        if isinstance(node, BinaryOp):
            return self._binary(node)
        if isinstance(node, Case):
            whens = [(self.compile(c), self.compile(r)) for c, r in node.whens]
            else_ = self.compile(node.else_) if node.else_ is not None else None
            return case(*whens, else_=else_)
        if isinstance(node, FuncCall):
            return self._func(node)
        raise TypeError(node)  # pragma: no cover

    def _literal(self, node: Literal) -> Expr:
        v = node.value
        coerced = self._checked.coerced.get(id(node))
        if coerced is not None and isinstance(v, str):
            parsed: date | datetime = datetime.fromisoformat(v)
            from sqlalchemy import DateTime

            return self._binder.bind(parsed, DateTime())
        if v is None:
            return null()
        if isinstance(v, bool):
            return self._binder.bind(v, Boolean())
        if isinstance(v, int):
            return self._binder.bind(v, Integer())
        if isinstance(v, Decimal):
            return self._binder.bind(v, Numeric(asdecimal=True))
        return self._binder.bind(v, String())

    def _binary(self, node: BinaryOp) -> Expr:
        left, right = self.compile(node.left), self.compile(node.right)
        op = node.op
        if op == "AND":
            return and_(left, right)
        if op == "OR":
            return or_(left, right)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return true_divide(left, right, self._type(node.left), self._type(node.right),
                               self._dialect)
        return {
            "=": left == right, "<>": left != right, "<": left < right,
            "<=": left <= right, ">": left > right, ">=": left >= right,
        }[op]

    def _func(self, node: FuncCall) -> Expr:
        name = node.name
        args = [self.compile(a) for a in node.args]
        types = [self._type(a) for a in node.args]
        d = self._dialect
        if name in ("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "MIN", "MAX"):
            return aggregate(name, args[0], types[0], d)
        if name == "ABS":
            return func.abs(args[0])
        if name == "ROUND":
            x = args[0]
            digits = args[1] if len(args) > 1 else self._binder.bind(0, Integer())
            if d is Dialect.POSTGRESQL:
                x = cast(x, Numeric())  # round(double precision, int) does not exist
            return func.round(x, digits)
        if name == "FLOOR":
            return Floor(args[0], sa_type(self._type(node)))
        if name == "CEILING":
            return Ceiling(args[0], sa_type(self._type(node)))
        if name == "COALESCE":
            return func.coalesce(*args)
        if name == "NULLIF":
            return func.nullif(*args)
        if name == "UPPER":
            return func.upper(args[0])
        if name == "LOWER":
            return func.lower(args[0])
        if name == "TRIM":
            return func.trim(args[0])
        if name == "LENGTH":
            return CharLength(args[0])
        if name == "CONCAT":
            empty = self._binder.bind("", String())
            parts = [func.coalesce(cast(a, String(4000)), empty) for a in args]
            out: Expr = parts[0]
            for p in parts[1:]:
                out = out.concat(p)
            return out
        if name in ("YEAR", "MONTH", "DAY"):
            return cast(extract(name.lower(), args[0]), Integer())
        if name == "DATE_DIFF_DAYS":
            return DateDiff(args[0], args[1])
        raise ValueError(f"unknown function {name}")  # pragma: no cover
