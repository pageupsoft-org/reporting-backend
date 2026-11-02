"""A tiny fixed schema for exercising the expression engine in isolation."""

from sqlalchemy import column, table
from sqlalchemy.sql.elements import quoted_name

from reporting.core import DataType, Dialect
from reporting.query._internal.compiler import column_type, sa_dialect
from reporting.query._internal.expr.checker import CalcInfo, Checker
from reporting.query._internal.expr.compiler import Binder, ExprCompiler
from reporting.query._internal.expr.parser import ExprError, parse

FIELDS = {
    "o": {"id": DataType.INTEGER, "total_amount": DataType.DECIMAL, "discount": DataType.DECIMAL,
          "status": DataType.TEXT, "ordered_at": DataType.DATETIME, "shipped_at": DataType.DATETIME,
          "paid": DataType.BOOLEAN, "rate": DataType.FLOAT},
    "c": {"id": DataType.INTEGER, "country": DataType.TEXT},
}
CALCS = {"revenue": CalcInfo(DataType.DECIMAL, True), "orders": CalcInfo(DataType.INTEGER, True),
         "days": CalcInfo(DataType.INTEGER, False, grouped=True),
         "loose": CalcInfo(DataType.INTEGER, False, grouped=False)}
GROUPED = {"c.country", "o.status"}
IDENTIFIERS = {name for cols in FIELDS.values() for name in cols} | set(FIELDS) | {"orders", "customers"}


def resolve_field(alias, col):
    try:
        return FIELDS[alias][col]
    except KeyError:
        raise ExprError(f"unknown field {alias}.{col}") from None


def resolve_calc(name):
    if name not in CALCS:
        raise ExprError(f"unknown column [{name}]")
    return CALCS[name]


def check(text):
    return Checker(resolve_field, resolve_calc, GROUPED).check(parse(text))


def compile_sql(text, dialect=Dialect.POSTGRESQL):
    node = parse(text)
    checked = Checker(resolve_field, resolve_calc, GROUPED).check(node)
    tables = {a: table(quoted_name("orders" if a == "o" else "customers", True),
                       *[column(quoted_name(n, True), column_type(t)) for n, t in cols.items()]
                       ).alias(a) for a, cols in FIELDS.items()}
    calc_exprs = {n: column(f"calc_{n}") for n in CALCS}
    expr = ExprCompiler(checked, dialect, Binder(), lambda a, c: tables[a].c[c],
                        lambda n: calc_exprs[n]).compile(node)
    compiled = expr.compile(dialect=sa_dialect(dialect))
    return str(compiled), dict(compiled.params), checked
