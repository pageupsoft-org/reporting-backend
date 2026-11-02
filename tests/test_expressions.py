import pytest

from reporting.core import DataType, Dialect
from reporting.query._internal.expr.parser import ExprError, parse
from tests.expr_helpers import check, compile_sql


@pytest.mark.parametrize("text,dtype,agg", [
    ("(o.total_amount - o.discount) * 1.18", DataType.DECIMAL, False),
    ("[revenue] / NULLIF([orders], 0)", DataType.DECIMAL, True),
    ("o.id + 1", DataType.INTEGER, False),
    ("o.id / 2", DataType.DECIMAL, False),
    ("o.rate * 2", DataType.FLOAT, False),
    ("o.total_amount > 100 AND NOT o.paid", DataType.BOOLEAN, False),
    ("o.ordered_at >= '2026-01-01'", DataType.BOOLEAN, False),
    ("CASE WHEN o.id > 2 THEN 'a' ELSE NULL END", DataType.TEXT, False),
    ("CASE WHEN o.paid THEN 1 ELSE 2.5 END", DataType.DECIMAL, False),
    ("COUNT_DISTINCT(c.country)", DataType.INTEGER, True),
    ("AVG(o.id)", DataType.DECIMAL, True),
    ("MAX(o.ordered_at)", DataType.DATETIME, True),
    ("SUM(o.total_amount) / COUNT(o.id)", DataType.DECIMAL, True),
    ("UPPER(c.country)", DataType.TEXT, False),
    ("CONCAT(c.country, o.id)", DataType.TEXT, False),
    ("YEAR(o.ordered_at)", DataType.INTEGER, False),
    ("DATE_DIFF_DAYS(o.ordered_at, o.shipped_at)", DataType.INTEGER, False),
    ("ROUND(o.total_amount, 2)", DataType.DECIMAL, False),
    ("COALESCE(o.discount, 0)", DataType.DECIMAL, False),
    ("SUM(o.total_amount) + LENGTH(c.country)", DataType.DECIMAL, True),  # c.country grouped
    ("[days] * 2", DataType.INTEGER, False),
    ("[revenue] + [days]", DataType.DECIMAL, True),
    ("case when o.id = 1 then true else false end", DataType.BOOLEAN, False),  # keywords: any case
])
def test_type_inference(text, dtype, agg):
    r = check(text)
    assert (r.data_type, r.aggregate) == (dtype, agg)


@pytest.mark.parametrize("text,fragment", [
    ("EXEC(1)", "unknown function 'EXEC'"),
    ("o.status + 1", "'+' needs a number, got text"),
    ("o.total_amount = 'x'", "Cannot compare a number with text"),
    ("o.id = NULL", "never true"),
    ("NOT o.id", "NOT needs true/false"),
    ("SUM(c.country)", "SUM needs a number"),
    ("SUM(SUM(o.id))", "cannot be nested"),
    ("SUM([revenue])", "already aggregated"),
    ("SUM(o.id) + o.total_amount", "o.total_amount must be inside an aggregate"),
    ("[revenue] + [loose]", "[loose] must be grouped"),
    ("CASE WHEN o.id THEN 1 END", "CASE WHEN condition needs true/false"),
    ("CASE WHEN o.paid THEN 1 ELSE 'x' END", "CASE branches must share a type"),
    ("ROUND(o.total_amount, o.id)", "whole number"),
    ("ABS(1, 2)", "ABS takes 1 argument"),
    ("CONCAT(c.country)", "CONCAT takes 2+"),
    ("YEAR(o.status)", "YEAR needs a date"),
    ("o.nope", "unknown field o.nope"),
    ("[nope]", "unknown column [nope]"),
    ("NULL", "always NULL"),
    ("o.paid < TRUE", "cannot order"),
])
def test_type_errors(text, fragment):
    with pytest.raises(ExprError) as exc:
        check(text)
    assert fragment in exc.value.message


@pytest.mark.parametrize("text,fragment", [
    ("1 +", "ends unexpectedly"),
    ("1; DROP TABLE x", "Unexpected character ';'"),
    ("o.x ** 2", "Unexpected '*'"),
    ("SELECT * FROM t", "Unexpected"),
    ("'unterminated", "Unexpected character"),
    ("o.total_amount -- comment", "ends unexpectedly"),
    ("x" * 501, "longer than 500"),
    ("", "empty"),
    ("+".join(["o.id"] * 60), "more than 100 parts"),
    ("ABS(" * 11 + "1" + ")" * 11, "nested more than 10"),
])
def test_parse_errors_and_limits(text, fragment):
    with pytest.raises(ExprError) as exc:
        parse(text)
    assert fragment in exc.value.message


def test_literals_become_binds_and_identifiers_are_quoted():
    sql, params, _ = compile_sql("CONCAT(c.country, 'x''; DROP TABLE t; --') = 'a'")
    assert "DROP" not in sql and 'c."country"' in sql
    assert "x'; DROP TABLE t; --" in params.values()


def test_true_division_per_dialect():
    pg, _, _ = compile_sql("o.id / 2")
    assert 'CAST(o."id" AS NUMERIC(38, 10)) / ' in pg
    lite, _, _ = compile_sql("o.id / 2", Dialect.SQLITE)
    assert "CAST(o.\"id\" AS FLOAT)" in lite


@pytest.mark.parametrize("dialect,fragment", [
    (Dialect.POSTGRESQL, 'round(CAST(o."total_amount" AS NUMERIC), :p1)'),
    (Dialect.MSSQL, "round(o.[total_amount], :p1)"),
])
def test_round(dialect, fragment):
    assert fragment in compile_sql("ROUND(o.total_amount, 2)", dialect)[0]


@pytest.mark.parametrize("dialect,fragment", [
    (Dialect.POSTGRESQL, 'CHAR_LENGTH(c."country")'),
    (Dialect.MSSQL, "LEN(c.[country])"),
    (Dialect.SQLITE, 'LENGTH(c."country")'),
])
def test_length(dialect, fragment):
    assert fragment in compile_sql("LENGTH(c.country)", dialect)[0]


def test_calc_refs_compile_to_referenced_expression():
    sql, _, _ = compile_sql("[revenue] / NULLIF([orders], 0)")
    assert "calc_revenue" in sql and "nullif(calc_orders, :p1)" in sql


def test_date_literal_coercion_binds_datetime():
    from datetime import datetime
    _, params, _ = compile_sql("o.ordered_at >= '2026-01-01'")
    assert params == {"p1": datetime(2026, 1, 1)}
