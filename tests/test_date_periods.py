from datetime import UTC, date, datetime

import pandas as pd
import pytest

from reporting.core import AccessContext, Dialect, ExecutionLimits, OutputColumn, QueryResult
from reporting.query import create_query_service, parse_spec
from reporting.query._internal.compiler import compile_plan
from reporting.query._internal.periods import bucket_starts, resolve_period
from reporting.query._internal.validator import Validator
from reporting.render import ChartSpec, TableOptions, create_render_service

# Thursday 23 April 2026, 22:30 UTC — already Friday 24th in Asia/Kolkata.
NOW = datetime(2026, 4, 23, 22, 30, tzinfo=UTC)
clock = lambda: NOW  # noqa: E731


@pytest.mark.parametrize("name,today,start,end", [
    ("today", date(2026, 4, 23), date(2026, 4, 23), date(2026, 4, 24)),
    ("last_7_days", date(2026, 4, 23), date(2026, 4, 17), date(2026, 4, 24)),
    ("last_30_days", date(2026, 3, 5), date(2026, 2, 4), date(2026, 3, 6)),   # across months
    ("last_90_days", date(2026, 1, 15), date(2025, 10, 18), date(2026, 1, 16)),
    ("this_week", date(2026, 4, 23), date(2026, 4, 20), date(2026, 4, 27)),   # Mon..Sun
    ("last_week", date(2026, 4, 23), date(2026, 4, 13), date(2026, 4, 20)),
    ("this_week", date(2026, 4, 20), date(2026, 4, 20), date(2026, 4, 27)),   # on a Monday
    ("this_month", date(2026, 12, 15), date(2026, 12, 1), date(2027, 1, 1)),
    ("last_month", date(2026, 1, 10), date(2025, 12, 1), date(2026, 1, 1)),   # across years
    ("last_6_months", date(2026, 3, 31), date(2025, 10, 1), date(2026, 4, 1)),
    ("this_year", date(2026, 7, 4), date(2026, 1, 1), date(2027, 1, 1)),
    ("last_year", date(2026, 7, 4), date(2025, 1, 1), date(2026, 1, 1)),
])
def test_resolve_period(name, today, start, end):
    assert resolve_period(name, today) == (start, end)


def test_bucket_starts():
    assert len(bucket_starts("day", date(2026, 4, 13), date(2026, 4, 20))) == 7
    assert bucket_starts("month", date(2025, 11, 1), date(2026, 2, 1)) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)]
    assert bucket_starts("year", date(2025, 1, 1), date(2026, 1, 1)) == [date(2025, 1, 1)]


def monthly(conn_id, period="last_6_months", grain="month", **extra):
    return {
        "connection_id": conn_id, "timezone": "UTC",
        "base": {"alias": "o", "table": {"database": "main", "name": "orders"}},
        "columns": [
            {"id": "month", "kind": "field", "ref": "o.ordered_at", "grain": grain, "label": "Month"},
            {"id": "revenue", "kind": "aggregate", "fn": "sum", "arg": "o.total_amount"},
            {"id": "orders", "kind": "aggregate", "fn": "count", "arg": "o.id"},
        ],
        "filters": [{"ref": "o.ordered_at", "op": "in_period", "value": period}],
        **extra,
    }


def test_validation_errors(metadata, sales_conn):
    v = Validator(metadata, clock)
    bad = monthly(sales_conn.id, timezone="Mars/Olympus")
    bad["columns"][0]["ref"] = "o.status"
    _, issues = v.run(parse_spec(bad), None)
    msgs = {i.path: i.message for i in issues}
    assert "Unknown time zone" in msgs["timezone"]
    assert "Grouping by month needs a date" in msgs["columns[0].grain"]
    unknown = monthly(sales_conn.id)
    unknown["filters"][0]["value"] = "fortnight"
    _, issues = v.run(parse_spec(unknown), None)
    assert "choose one of" in {i.path: i.message for i in issues}["filters[0].value"]
    wrong = monthly(sales_conn.id)
    wrong["filters"][0]["ref"] = "o.total_amount"
    _, issues = v.run(parse_spec(wrong), None)
    assert any("needs a date column" in i.message for i in issues)


def test_timezone_decides_today(metadata, sales_conn):
    v = Validator(metadata, clock)
    utc, _ = v.run(parse_spec(monthly(sales_conn.id, "today")), None)
    ist, _ = v.run(parse_spec(monthly(sales_conn.id, "today", timezone="Asia/Kolkata")), None)
    assert utc.filters[0].period[0] == date(2026, 4, 23)
    assert ist.filters[0].period[0] == date(2026, 4, 24)


@pytest.mark.parametrize("dialect,grain,fragment", [
    (Dialect.POSTGRESQL, "month", """CAST(date_trunc('month', o."ordered_at") AS DATE)"""),
    (Dialect.MSSQL, "month", "DATEFROMPARTS(YEAR(o.[ordered_at]), MONTH(o.[ordered_at]), 1)"),
    (Dialect.MSSQL, "day", "CAST(o.[ordered_at] AS DATE)"),
    (Dialect.MYSQL, "year", "MAKEDATE(YEAR(o.`ordered_at`), 1)"),
    (Dialect.SQLITE, "month", """date(o."ordered_at", 'start of month')"""),
])
def test_date_trunc_sql(metadata, sales_conn, dialect, grain, fragment):
    plan, issues = Validator(metadata, clock).run(parse_spec(monthly(sales_conn.id, grain=grain)), None)
    assert plan, issues
    sql = compile_plan(plan, dialect).statement_sql
    assert fragment in sql
    assert sql.count(fragment) == 2  # selected and grouped
    assert "o.ordered_at >= " in sql.replace('"', "").replace("[", "").replace("]", "").replace("`", "")


def run(connections, metadata, raw):
    qs = create_query_service(metadata, connections, clock=clock)
    return qs.execute(parse_spec(raw), AccessContext.unrestricted(raw["connection_id"]),
                      ExecutionLimits())


def test_last_6_months_by_month_fills_empty_months(connections, metadata, sales_conn):
    # Sample orders exist in Jan–Apr 2026; Nov and Dec 2025 have none.
    r = run(connections, metadata, monthly(sales_conn.id))
    f = r.frame
    assert [d.strftime("%Y-%m") for d in f["month"]] == [
        "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]
    assert list(f["orders"]) == [0, 0, 1, 1, 2, 1]
    assert list(f["revenue"]) == [0, 0, 100.0, 250.0, 115.5, 300.0]
    assert r.columns[0].grain == "month"


def test_this_year_by_month_desc_order(connections, metadata, sales_conn):
    raw = monthly(sales_conn.id, "this_year", order_by=[{"ref": "month", "dir": "desc"}])
    f = run(connections, metadata, raw).frame
    assert len(f) == 12 and f["month"].iloc[0] == pd.Timestamp("2026-12-01")


def test_last_week_by_day(connections, metadata, sales_conn):
    f = run(connections, metadata, monthly(sales_conn.id, "last_week", grain="day")).frame
    assert len(f) == 7 and f["orders"].sum() == 0  # 13–19 April: no orders, still 7 days


def test_no_gap_fill_with_second_dimension(connections, metadata, sales_conn):
    raw = monthly(sales_conn.id)
    raw["columns"].insert(1, {"id": "status", "kind": "field", "ref": "o.status"})
    f = run(connections, metadata, raw).frame
    assert len(f) == 5  # only real (month, status) groups


def test_render_buckets():
    frame = pd.DataFrame({"m": pd.to_datetime(["2026-02-01", "2026-01-01"]), "n": [3, 5]})
    res = QueryResult(frame=frame, columns=(
        OutputColumn(id="m", label="Month", data_type="date", role="dimension", grain="month"),
        OutputColumn(id="n", label="Orders", data_type="integer", role="measure")),
        row_count=2, truncated=False, elapsed_ms=1)
    render = create_render_service()
    assert render.table(res, TableOptions()).display[0] == ["Feb 2026", "3"]
    chart = render.chart(res, ChartSpec(type="bar", x="m", y=("n",)))
    assert chart.option["xAxis"]["data"] == ["Jan 2026", "Feb 2026"]  # chronological
    assert chart.option["series"][0]["data"] == [5.0, 3.0]
    assert render.suggest_charts(res)[0].type == "bar"
