import re

import pytest

from reporting.core import Dialect
from reporting.query import parse_spec
from reporting.query._internal.compiler import compile_plan
from reporting.query._internal.validator import Validator
from tests.spec_fixtures import example_spec


@pytest.fixture
def plan(metadata, sales_conn):
    def make(raw):
        p, issues = Validator(metadata).run(parse_spec(raw), None)
        assert p is not None, issues
        return p
    return make


def sql_for(plan, raw, dialect):
    return compile_plan(plan(raw), dialect)


def test_postgres_matches_plan_example(plan, sales_conn):
    cq = sql_for(plan, example_spec(sales_conn.id), Dialect.POSTGRESQL)
    sql = " ".join(cq.statement_sql.split())
    assert 'SELECT c."country" AS country, sum(o."total_amount") AS revenue' in sql
    assert 'avg((CAST(o."shipped_at" AS DATE) - CAST(o."ordered_at" AS DATE))) AS avg_ship' in sql
    assert 'LEFT OUTER JOIN "customers" AS c ON o."customer_id" = c."id"' in sql
    assert 'WHERE o."status" IN (:p2_1, :p2_2) AND o."ordered_at" BETWEEN :p3 AND :p4' in sql
    assert 'GROUP BY c."country" HAVING sum(o."total_amount") > :p5' in sql
    assert sql.endswith("ORDER BY revenue DESC LIMIT :p6")
    assert cq.params["p2_1"] == "paid" and cq.params["p6"] == 1000


@pytest.mark.parametrize("dialect,fragment", [
    (Dialect.POSTGRESQL, 'CAST(o."shipped_at" AS DATE) - CAST(o."ordered_at" AS DATE)'),
    (Dialect.MSSQL, "DATEDIFF(day, o.[ordered_at], o.[shipped_at])"),
    (Dialect.MYSQL, "DATEDIFF(o.`shipped_at`, o.`ordered_at`)"),
    (Dialect.SQLITE, 'julianday(date(o."shipped_at")) - julianday(date(o."ordered_at"))'),
])
def test_date_diff_per_dialect(plan, sales_conn, dialect, fragment):
    assert fragment in sql_for(plan, example_spec(sales_conn.id), dialect).statement_sql


def test_mssql_avg_int_cast_and_top(plan, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["columns"].append({"id": "q", "kind": "aggregate", "fn": "avg", "arg": "o.id"})
    raw["columns"].append({"id": "s", "kind": "aggregate", "fn": "sum", "arg": "o.id"})
    sql = sql_for(plan, raw, Dialect.MSSQL).statement_sql
    assert "avg(CAST(o.[id] AS NUMERIC(38, 6)))" in sql
    assert "sum(CAST(o.[id] AS BIGINT))" in sql
    assert sql.startswith("SELECT TOP 1000 ")
    pg = sql_for(plan, raw, Dialect.POSTGRESQL).statement_sql
    assert 'avg(o."id")' in pg


def test_count_star_count_distinct_min_max(plan, sales_conn):
    raw = example_spec(sales_conn.id, columns=[
        {"id": "st", "kind": "field", "ref": "o.status"},
        {"id": "n", "kind": "aggregate", "fn": "count"},
        {"id": "cd", "kind": "aggregate", "fn": "count_distinct", "arg": "o.customer_id"},
        {"id": "first", "kind": "aggregate", "fn": "min", "arg": "o.ordered_at"},
        {"id": "last", "kind": "aggregate", "fn": "max", "arg": "o.ordered_at"},
    ], having=[], order_by=[{"ref": "st"}])
    sql = sql_for(plan, raw, Dialect.POSTGRESQL).statement_sql
    for frag in ("count(*) AS n", 'count(DISTINCT o."customer_id") AS cd',
                 'min(o."ordered_at") AS first', 'max(o."ordered_at") AS last',
                 'GROUP BY o."status"', "ORDER BY st ASC"):
        assert frag in sql


def test_no_aggregates_means_no_group_by(plan, sales_conn):
    raw = example_spec(sales_conn.id, columns=[
        {"id": "id", "kind": "field", "ref": "o.id"},
        {"id": "days", "kind": "date_diff", "start": "o.ordered_at", "end": "o.shipped_at"},
    ], having=[], order_by=[{"ref": "o.id", "dir": "desc"}], filters=[])
    sql = sql_for(plan, raw, Dialect.POSTGRESQL).statement_sql
    assert "GROUP BY" not in sql and 'ORDER BY o."id" DESC' in sql


def test_values_are_never_inlined(plan, sales_conn):
    evil = "x'; DROP TABLE orders; --"
    raw = example_spec(sales_conn.id)
    raw["filters"] = [{"ref": "o.status", "op": "eq", "value": evil},
                      {"ref": "c.country", "op": "like", "value": "%" + evil}]
    raw["columns"].append({"id": "e", "kind": "expression",
                           "expr": "CONCAT(c.country, '" + evil.replace("'", "''") + "')"})
    for d in Dialect:
        cq = sql_for(plan, raw, d)
        assert "DROP" not in cq.statement_sql
        assert evil in cq.params.values()


def test_schema_qualification(plan, sales_conn, metadata):
    # Fake a PostgreSQL-style schema to check quoting of schema-qualified names.
    p = plan(example_spec(sales_conn.id))
    t = p.aliases["o"]
    p.aliases["o"] = t.model_copy(update={"schema_name": "public"})
    sql = compile_plan(p, Dialect.POSTGRESQL).statement_sql
    assert 'FROM "public"."orders" AS o' in sql
    assert 'FROM [public].[orders] AS o' in compile_plan(p, Dialect.MSSQL).statement_sql
    assert "FROM `orders` AS o" in compile_plan(p, Dialect.MYSQL).statement_sql


def test_placeholders_are_named(plan, sales_conn):
    for d in Dialect:
        sql = sql_for(plan, example_spec(sales_conn.id), d).statement_sql
        assert "%(" not in sql and "?" not in sql and "::" not in sql
        assert re.search(r":p\d+", sql)
