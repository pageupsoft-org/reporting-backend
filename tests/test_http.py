import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from reporting.app.config import Settings
from reporting.app.main import create_app
from reporting.connections import generate_master_key
from tests.spec_fixtures import example_spec


@pytest.fixture
def client(tmp_path):
    settings = Settings(app_db_url=f"sqlite:///{tmp_path / 'app.db'}",
                        master_key=generate_master_key(), jwt_secret="j" * 40,
                        admin_password="admin-pass-1", data_dir=tmp_path)
    return TestClient(create_app(settings))


def login(client, user="admin", password="admin-pass-1"):
    r = client.post("/api/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin(client):
    return login(client)


@pytest.fixture
def conn_id(client, admin, sales_db):
    r = client.post("/api/connections", headers=admin, json={
        "name": "Sales", "dialect": "sqlite", "connection_string": f"sqlite:///{sales_db}"})
    assert r.status_code == 201, r.text
    assert "connection_string" not in r.json()
    return r.json()["id"]


def test_requires_auth(client):
    assert client.get("/api/connections").status_code == 401
    r = client.get("/api/connections", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"
    assert client.get("/api/health").json() == {"status": "ok"}


def test_browse_schema(client, admin, conn_id):
    assert [c["id"] for c in client.get("/api/connections", headers=admin).json()] == [conn_id]
    dbs = client.get(f"/api/connections/{conn_id}/databases", headers=admin).json()
    assert dbs == [{"connection_id": conn_id, "database": "main"}]
    tables = client.get(f"/api/connections/{conn_id}/databases/main/tables", headers=admin).json()
    assert tables["snapshot_version"] == 1
    assert {t["ref"]["name"] for t in tables["tables"]} >= {"orders", "customers"}
    t = client.get("/api/tables", headers=admin,
                   params={"connection_id": conn_id, "database": "main", "name": "orders"}).json()
    assert {r["related_table"]["name"] for r in t["outgoing"]} == {"customers"}
    assert {r["related_table"]["name"] for r in t["incoming"]} == {"order_items", "shipments"}
    paths = client.get("/api/join-paths", headers=admin, params={
        "connection_id": conn_id, "database": "main", "from": "orders", "to": "regions"}).json()
    assert paths[0]["tables"] == ["orders", "customers", "regions"]


def test_query_flow(client, admin, conn_id):
    spec = example_spec(conn_id)
    rep = client.post("/api/query/validate", headers=admin, json=spec).json()
    assert rep["valid"] and len(rep["output_columns"]) == 5
    bad = client.post("/api/query/validate", headers=admin,
                      json={**spec, "columns": [{"kind": "field", "id": "x", "ref": "o.nope"}]}).json()
    assert not bad["valid"] and bad["issues"][0]["path"] == "columns[0].ref"
    shape = client.post("/api/query/validate", headers=admin, json={**spec, "limit": -1}).json()
    assert not shape["valid"] and shape["issues"][0]["path"] == "limit"
    sql = client.post("/api/query/sql", headers=admin, json=spec).json()
    assert sql["sql"].startswith("SELECT") and sql["dialect"] == "sqlite"
    prev = client.post("/api/query/preview", headers=admin, json=spec).json()
    assert prev["table"]["display"][0] == ["Germany", "350.00", "2", "2.00", "175.00"]
    assert prev["suggested_charts"][0]["type"] == "bar"
    run = client.post("/api/query/run", headers=admin, json={"query": spec}).json()
    assert run["row_count"] == 3 and run["cache_hit"] is False  # preview used a smaller LIMIT
    again = client.post("/api/query/run", headers=admin, json={"query": spec}).json()
    assert again["cache_hit"] is True
    fresh = client.post("/api/query/run", headers=admin, json={"query": spec, "refresh": True})
    assert fresh.json()["cache_hit"] is False
    chart = client.post("/api/render/chart", headers=admin, json={
        "query": spec, "chart": {"type": "bar", "x": "country", "y": ["revenue"]}}).json()
    assert chart["option"]["series"][0]["data"] == [350.0, 300.0, 75.5]
    xlsx = client.post("/api/render/export", headers=admin, json={"query": spec, "format": "xlsx"})
    assert xlsx.status_code == 200 and "attachment" in xlsx.headers["content-disposition"]
    assert len(pd.read_excel(io.BytesIO(xlsx.content))) == 3


def test_error_mapping(client, admin, conn_id):
    spec = example_spec(conn_id, snapshot_version=99)
    r = client.post("/api/query/preview", headers=admin, json=spec)
    assert r.status_code == 404  # snapshot 99 never existed
    r = client.post("/api/query/preview", headers=admin,
                    json={**example_spec(conn_id), "columns": [{"kind": "field", "id": "x", "ref": "o.nope"}]})
    assert r.status_code == 422 and r.json()["issues"][0]["path"] == "columns[0].ref"
    assert client.get("/api/tables", headers=admin, params={
        "connection_id": conn_id, "database": "main", "name": "nope"}).status_code == 404


def test_permissions_and_column_policies(client, admin, conn_id):
    for name, role in (("vic", "viewer"), ("ana", "analyst")):
        r = client.post("/api/users", headers=admin,
                        json={"username": name, "password": "password1", "roles": [role]})
        assert r.status_code == 201, r.text
    vic = login(client, "vic", "password1")
    assert client.get("/api/connections", headers=vic).json() == []
    assert client.post("/api/query/preview", headers=vic,
                       json=example_spec(conn_id)).status_code == 403
    assert client.post(f"/api/connections/{conn_id}/grants", headers=admin,
                       json={"subject": "role:viewer"}).status_code == 204
    assert client.post(f"/api/connections/{conn_id}/column-policies", headers=admin, json={
        "connection_id": conn_id, "role": "viewer", "table_key": "customers",
        "column": "country"}).status_code == 204
    cols = client.get("/api/tables", headers=vic, params={
        "connection_id": conn_id, "database": "main", "name": "customers"}).json()["columns"]
    assert "country" not in {c["name"] for c in cols}
    r = client.post("/api/query/preview", headers=vic, json=example_spec(conn_id))
    assert r.status_code == 422 and "not visible to your role" in r.json()["message"]
    assert client.delete(f"/api/connections/{conn_id}", headers=vic).status_code == 403
    assert client.post("/api/users", headers=vic, json={
        "username": "x1x", "password": "password1"}).status_code == 403
    assert client.get("/api/openapi.json").status_code == 200
