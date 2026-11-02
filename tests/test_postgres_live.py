"""Live PostgreSQL tests: the app database on PostgreSQL, introspection of a real server, and
golden cross-engine results (SQLite vs PostgreSQL on identical data).

Run with a server that has the demo ``sales`` data and an empty app database:
    REPORTING_TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:55432
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from reporting.app.config import Settings
from reporting.app.main import create_app
from reporting.connections import generate_master_key

BASE = os.environ.get("REPORTING_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="REPORTING_TEST_POSTGRES_URL not set")
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo" / "sales.db"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import psycopg
    admin = BASE.replace("postgresql://", "postgresql://", 1) + "/postgres"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("drop database if exists reporting_app_test")
        c.execute("create database reporting_app_test")
    tmp = tmp_path_factory.mktemp("pg")
    settings = Settings(app_db_url=BASE.replace("postgresql://", "postgresql+psycopg://", 1) + "/reporting_app_test",
                        master_key=generate_master_key(), jwt_secret="j" * 40,
                        admin_password="admin-pass-1", data_dir=tmp)
    return TestClient(create_app(settings))


@pytest.fixture(scope="module")
def auth(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def conns(client, auth):
    out = {}
    for name, dialect, cs in [("pg", "postgresql", f"{BASE}/sales"),
                              ("lite", "sqlite", f"sqlite:///{DEMO}")]:
        r = client.post("/api/connections", headers=auth, json={"name": name, "dialect": dialect, "connection_string": cs})
        assert r.status_code == 201, r.text
        out[name] = r.json()["id"]
    return out


def test_app_database_on_postgres(client, auth, conns):
    assert client.get("/api/ready").json() == {"status": "ready"}
    test = client.post(f"/api/connections/{conns['pg']}/test", headers=auth).json()
    assert test["ok"], test
    assert {s["name"] for s in test["steps"]} >= {"dns", "tcp", "login", "database", "write_check"}


def test_postgres_introspection(client, auth, conns):
    dbs = [d["database"] for d in client.get(f"/api/connections/{conns['pg']}/databases", headers=auth).json()]
    assert "sales" in dbs
    tables = client.get(f"/api/connections/{conns['pg']}/databases/sales/tables", headers=auth).json()["tables"]
    keys = {f"{t['ref']['schema']}.{t['ref']['name']}" for t in tables}
    assert {"public.orders", "public.customers", "finance.invoices"} <= keys
    detail = client.get("/api/tables", headers=auth, params={
        "connection_id": conns["pg"], "database": "sales", "schema": "public", "name": "orders"}).json()
    types = {c["name"]: c["data_type"] for c in detail["columns"]}
    assert types["ordered_at"] == "datetime_tz" and types["total_amount"] == "decimal"
    assert {r["related_table"]["name"] for r in detail["outgoing"]} == {"customers"}
    incoming = {f"{r['related_table']['schema']}.{r['related_table']['name']}": r["source"] for r in detail["incoming"]}
    assert incoming["finance.invoices"] == "declared"  # cross-schema FK
    assert incoming["public.shipments"] == "inferred"
    cust = client.get("/api/tables", headers=auth, params={
        "connection_id": conns["pg"], "database": "sales", "schema": "public", "name": "customers"}).json()
    ctypes = {c["name"]: c["data_type"] for c in cust["columns"]}
    assert ctypes["external_id"] == "uuid" and ctypes["vip"] == "boolean" and ctypes["created_at"] == "datetime"


def spec(conn_id, schema, database, **kw):
    t = lambda name: {"database": database, "schema": schema, "name": name}  # noqa: E731
    return {"connection_id": conn_id, "timezone": "UTC",
            "base": {"alias": "o", "table": t("orders")}, **kw}


def run(client, auth, body):
    r = client.post("/api/query/run", headers=auth, json={"query": body, "page_size": 1000})
    assert r.status_code == 200, r.text
    t = r.json()["table"]
    return pd.DataFrame(t["rows"], columns=[c["id"] for c in t["columns"]]), r.json()["sql"]


GOLDEN = {
    "country_revenue": dict(
        joins=[{"alias": "c", "from_alias": "o", "relationship_id": "REL", "type": "left"}],
        columns=[{"id": "country", "kind": "field", "ref": "c.country"},
                 {"id": "revenue", "kind": "aggregate", "fn": "sum", "arg": "o.total_amount"},
                 {"id": "orders", "kind": "aggregate", "fn": "count", "arg": "o.id"},
                 {"id": "avg_ship", "kind": "aggregate", "fn": "avg",
                  "arg": {"kind": "date_diff", "unit": "day", "start": "o.ordered_at", "end": "o.shipped_at"}},
                 {"id": "aov", "kind": "expression", "expr": "ROUND([revenue] / NULLIF([orders], 0), 2)"}],
        filters=[{"ref": "o.status", "op": "in", "value": ["paid", "shipped"]}],
        order_by=[{"ref": "country"}]),
    "monthly_2026": dict(
        columns=[{"id": "month", "kind": "field", "ref": "o.ordered_at", "grain": "month"},
                 {"id": "revenue", "kind": "aggregate", "fn": "sum", "arg": "o.total_amount"},
                 {"id": "n", "kind": "aggregate", "fn": "count"}],
        filters=[{"ref": "o.ordered_at", "op": "between", "value": ["2026-01-01", "2026-07-01"]}],
        order_by=[{"ref": "month"}]),
    "expressions": dict(
        columns=[{"id": "id", "kind": "field", "ref": "o.id"},
                 {"id": "net", "kind": "expression", "expr": "ROUND(o.total_amount - o.discount, 2)"},
                 {"id": "yr", "kind": "expression", "expr": "YEAR(o.ordered_at) * 100 + MONTH(o.ordered_at)"},
                 {"id": "days", "kind": "date_diff", "start": "o.ordered_at", "end": "o.shipped_at"},
                 {"id": "label", "kind": "expression", "expr": "UPPER(CONCAT(o.status, '-', o.id))"},
                 {"id": "bucket", "kind": "expression",
                  "expr": "CASE WHEN o.total_amount >= 1000 THEN 'big' ELSE 'small' END"}],
        filters=[{"ref": "o.id", "op": "lte", "value": 200}],
        order_by=[{"ref": "id"}]),
}


@pytest.mark.parametrize("name", list(GOLDEN))
def test_golden_results_match_across_engines(client, auth, conns, name):
    body = GOLDEN[name]
    frames = {}
    for engine, conn, schema, database in (("pg", conns["pg"], "public", "sales"),
                                           ("lite", conns["lite"], None, "main")):
        b = {**body}
        if "joins" in b:
            rel = "orders_customer_id_fkey" if engine == "pg" else "fk_orders_customer_id"
            b["joins"] = [{**j, "relationship_id": rel} for j in b["joins"]]
        frames[engine], sql = run(client, auth, spec(conn, schema, database, **b))
        if engine == "pg":
            assert '"public"."orders"' in sql
    pg, lite = frames["pg"], frames["lite"]
    assert len(pg) == len(lite) > 0
    for col in pg.columns:
        a, b = pg[col], lite[col]
        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
            pd.testing.assert_series_equal(a.astype(float).round(2), b.astype(float).round(2),
                                           check_names=False, obj=col)
        else:
            assert a.astype(str).str[:10].tolist() == b.astype(str).str[:10].tolist(), col


def test_reports_and_embeds_on_postgres_app_db(client, auth, conns):
    body = spec(conns["pg"], "public", "sales", columns=[
        {"id": "status", "kind": "field", "ref": "o.status"}, {"id": "n", "kind": "aggregate", "fn": "count"}],
        order_by=[{"ref": "status"}])
    doc = {"elements": [{"id": "t", "type": "table", "layout": {"x": 0, "y": 0, "w": 6, "h": 6},
                         "binding": {"query": body}}],
           "parameters": [{"id": "date_range", "label": "Date range", "default": "this_year",
                           "targets": [{"element_id": "t", "ref": "o.ordered_at"}]}]}
    r = client.post("/api/reports", headers=auth, json={"name": "PG", "kind": "template", "document": doc})
    assert r.status_code == 201, r.text
    rid, version = r.json()["id"], r.json()["version"]
    stale = client.put(f"/api/reports/{rid}", headers=auth, json={"name": "x", "kind": "template", "document": doc, "version": version + 5})
    assert stale.status_code == 409
    e = client.post(f"/api/reports/{rid}/embeds", headers=auth, json={"element_id": "t", "mode": "public"}).json()
    data = client.get(f"/api/embed/{e['embed']['id']}?p.date_range=last_year", headers={"X-Embed-Load": "1"})
    assert data.status_code == 200, data.text
    assert sum(row[1] for row in data.json()["table"]["rows"]) > 0
