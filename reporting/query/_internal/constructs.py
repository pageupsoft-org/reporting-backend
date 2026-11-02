"""Custom SQLAlchemy constructs with one ``@compiles`` function per dialect."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Date, Integer, Numeric
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement


class DateDiff(ColumnElement[int]):
    """Number of calendar-day boundaries from ``start`` to ``end`` (time of day ignored)."""

    inherit_cache = True
    type = Integer()

    def __init__(self, start: ColumnElement[Any], end: ColumnElement[Any]) -> None:
        self.start = start
        self.end = end

    def get_children(self, **kw: Any) -> tuple[ColumnElement[Any], ...]:
        return (self.start, self.end)


@compiles(DateDiff)
@compiles(DateDiff, "postgresql")
def _date_diff_pg(el: DateDiff, compiler: SQLCompiler, **kw: Any) -> str:
    s, e = compiler.process(el.start, **kw), compiler.process(el.end, **kw)
    return f"(CAST({e} AS DATE) - CAST({s} AS DATE))"


@compiles(DateDiff, "mssql")
def _date_diff_mssql(el: DateDiff, compiler: SQLCompiler, **kw: Any) -> str:
    s, e = compiler.process(el.start, **kw), compiler.process(el.end, **kw)
    return f"DATEDIFF(day, {s}, {e})"


@compiles(DateDiff, "mysql")
def _date_diff_mysql(el: DateDiff, compiler: SQLCompiler, **kw: Any) -> str:
    s, e = compiler.process(el.start, **kw), compiler.process(el.end, **kw)
    return f"DATEDIFF({e}, {s})"  # note the reversed argument order


@compiles(DateDiff, "sqlite")
def _date_diff_sqlite(el: DateDiff, compiler: SQLCompiler, **kw: Any) -> str:
    s, e = compiler.process(el.start, **kw), compiler.process(el.end, **kw)
    return f"CAST(julianday(date({e})) - julianday(date({s})) AS INTEGER)"


class _Unary(FunctionElement[Any]):
    inherit_cache = True

    def __init__(self, arg: ColumnElement[Any], type_: Any = None) -> None:
        super().__init__(arg)
        if type_ is not None:
            self.type = type_


class Floor(_Unary):
    name = "floor"
    inherit_cache = True


class Ceiling(_Unary):
    name = "ceiling"
    inherit_cache = True


class CharLength(_Unary):
    name = "char_length"
    inherit_cache = True
    type = Integer()


def _arg(el: FunctionElement[Any], compiler: SQLCompiler, **kw: Any) -> str:
    return compiler.process(list(el.clauses)[0], **kw)


@compiles(Floor)
def _floor(el: Floor, compiler: SQLCompiler, **kw: Any) -> str:
    return f"FLOOR({_arg(el, compiler, **kw)})"


@compiles(Ceiling)
def _ceiling(el: Ceiling, compiler: SQLCompiler, **kw: Any) -> str:
    return f"CEILING({_arg(el, compiler, **kw)})"


@compiles(Floor, "sqlite")
def _floor_sqlite(el: Floor, compiler: SQLCompiler, **kw: Any) -> str:
    x = _arg(el, compiler, **kw)
    return f"(CAST({x} AS INTEGER) - ({x} < CAST({x} AS INTEGER)))"


@compiles(Ceiling, "sqlite")
def _ceiling_sqlite(el: Ceiling, compiler: SQLCompiler, **kw: Any) -> str:
    x = _arg(el, compiler, **kw)
    return f"(CAST({x} AS INTEGER) + ({x} > CAST({x} AS INTEGER)))"


@compiles(CharLength)
def _char_length(el: CharLength, compiler: SQLCompiler, **kw: Any) -> str:
    return f"CHAR_LENGTH({_arg(el, compiler, **kw)})"


@compiles(CharLength, "mssql")
def _char_length_mssql(el: CharLength, compiler: SQLCompiler, **kw: Any) -> str:
    return f"LEN({_arg(el, compiler, **kw)})"


@compiles(CharLength, "sqlite")
def _char_length_sqlite(el: CharLength, compiler: SQLCompiler, **kw: Any) -> str:
    return f"LENGTH({_arg(el, compiler, **kw)})"


DECIMAL_38_6 = Numeric(38, 6)


class DateTrunc(ColumnElement[Any]):
    """Start of the day, month or year containing ``expr``, as a DATE."""

    inherit_cache = True
    type = Date()

    def __init__(self, grain: str, expr: ColumnElement[Any]) -> None:
        if grain not in ("day", "month", "year"):
            raise ValueError(grain)
        self.grain = grain
        self.expr = expr

    def get_children(self, **kw: Any) -> tuple[ColumnElement[Any], ...]:
        return (self.expr,)


# The grain is one of three fixed words checked above, never user text.
@compiles(DateTrunc)
@compiles(DateTrunc, "postgresql")
def _trunc_pg(el: DateTrunc, compiler: SQLCompiler, **kw: Any) -> str:
    return f"CAST(date_trunc('{el.grain}', {compiler.process(el.expr, **kw)}) AS DATE)"


@compiles(DateTrunc, "mssql")
def _trunc_mssql(el: DateTrunc, compiler: SQLCompiler, **kw: Any) -> str:
    x = compiler.process(el.expr, **kw)
    if el.grain == "day":
        return f"CAST({x} AS DATE)"
    if el.grain == "month":
        return f"DATEFROMPARTS(YEAR({x}), MONTH({x}), 1)"
    return f"DATEFROMPARTS(YEAR({x}), 1, 1)"


@compiles(DateTrunc, "mysql")
def _trunc_mysql(el: DateTrunc, compiler: SQLCompiler, **kw: Any) -> str:
    x = compiler.process(el.expr, **kw)
    if el.grain == "day":
        return f"DATE({x})"
    if el.grain == "month":
        return f"DATE_SUB(DATE({x}), INTERVAL DAYOFMONTH({x}) - 1 DAY)"
    return f"MAKEDATE(YEAR({x}), 1)"


@compiles(DateTrunc, "sqlite")
def _trunc_sqlite(el: DateTrunc, compiler: SQLCompiler, **kw: Any) -> str:
    x = compiler.process(el.expr, **kw)
    if el.grain == "day":
        return f"date({x})"
    return f"date({x}, 'start of {el.grain}')"
