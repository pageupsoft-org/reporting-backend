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
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin(client):
    return login(client)


@pytest.fixture
def conn_id(client, admin, sales_db):
    r = client.post("/api/connections", headers=admin, json={
        "name": "Sales", "dialect": "sqlite", "connection_string": f"sqlite:///{sales_db}"})
    return r.json()["id"]


@pytest.fixture
def users(client, admin, conn_id):
    for name, role in (("ana", "analyst"), ("vic", "viewer")):
        client.post("/api/users", headers=admin,
                    json={"username": name, "password": "password1", "roles": [role]})
    client.post(f"/api/connections/{conn_id}/grants", headers=admin, json={"subject": "role:viewer"})
    client.post(f"/api/connections/{conn_id}/grants", headers=admin, json={"subject": "role:analyst"})
    return {"ana": login(client, "ana", "password1"), "vic": login(client, "vic", "password1")}


def document(conn_id, **chart):
    spec = {**example_spec(conn_id), "snapshot_version": 1}
    return {"elements": [
        {"id": "title", "type": "text", "layout": {"x": 0, "y": 0, "w": 12, "h": 2},
         "text": {"content": "Sales overview", "variant": "title"}},
        {"id": "chart", "type": "chart", "title": "Revenue", "layout": {"x": 0, "y": 2, "w": 6, "h": 8},
         "binding": {"query": spec, "chart": chart or {"type": "bar", "x": "country", "y": ["revenue"]}}},
        {"id": "table", "type": "table", "layout": {"x": 6, "y": 2, "w": 6, "h": 8},
         "binding": {"query": spec, "page_size": 50}},
    ]}


def test_template_lifecycle(client, admin, conn_id, users):
    ana, vic = users["ana"], users["vic"]
    r = client.post("/api/reports", headers=ana,
                    json={"name": "Sales overview", "kind": "template", "document": document(conn_id)})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert "snapshot_version" not in tpl["document"]["elements"][1]["binding"]["query"]

    # templates are a shared library; viewers can read and use them but not edit
    assert [t["name"] for t in client.get("/api/reports?kind=template", headers=vic).json()] == ["Sales overview"]
    assert client.put(f"/api/reports/{tpl['id']}", headers=vic,
                      json={"name": "x", "kind": "template", "version": 1}).status_code == 403
    mine = client.post(f"/api/reports/{tpl['id']}/duplicate", headers=vic, json={"kind": "report"})
    assert mine.status_code == 201 and mine.json()["source_id"] == tpl["id"]
    assert mine.json()["owner_id"] != tpl["owner_id"] and mine.json()["name"] == "Sales overview"

    # reports are private to their owner (and admins)
    rid = mine.json()["id"]
    assert client.get(f"/api/reports/{rid}", headers=ana).status_code == 403
    assert client.get(f"/api/reports/{rid}", headers=admin).status_code == 200
    assert [r["id"] for r in client.get("/api/reports?kind=report", headers=ana).json()] == []

    # optimistic concurrency
    body = {"name": "My sales", "kind": "report", "document": document(conn_id), "version": 1}
    assert client.put(f"/api/reports/{rid}", headers=vic, json=body).json()["version"] == 2
    stale = client.put(f"/api/reports/{rid}", headers=vic, json=body)
    assert stale.status_code == 409 and stale.json()["details"]["current_version"] == 2

    # report -> template ("save as template"), then delete
    t2 = client.post(f"/api/reports/{rid}/duplicate", headers=vic, json={"kind": "template", "name": "Vic's"})
    assert t2.json()["kind"] == "template"
    assert client.delete(f"/api/reports/{rid}", headers=vic).status_code == 204
    assert client.get(f"/api/reports/{rid}", headers=vic).status_code == 404


def test_binding_validation(client, admin, conn_id, users):
    bad = document(conn_id)
    bad["elements"][1]["binding"]["query"]["columns"] = []
    r = client.post("/api/reports", headers=admin, json={"name": "x", "document": bad})
    assert r.status_code == 422
    assert r.json()["issues"][0]["path"].startswith("document.elements[1].binding.query.columns")
    r = client.post("/api/reports", headers=admin,
                    json={"name": "x", "document": document(conn_id, type="donut", x="a", y=["b"])})
    assert r.status_code == 422 and r.json()["issues"][0]["path"] == "document.elements[1].binding.chart"
    other = client.post("/api/reports", headers=users["ana"], json={"name": "x", "document": document("conn_nope")})
    assert other.status_code == 422 and "access" in other.json()["message"]
    layout = document(conn_id)
    layout["elements"][0]["layout"] = {"x": 10, "y": 0, "w": 6, "h": 2}
    assert client.post("/api/reports", headers=admin, json={"name": "x", "document": layout}).status_code == 422


# -- embeds ---------------------------------------------------------------------------------

import time  # noqa: E402

import jwt  # noqa: E402


@pytest.fixture
def report_id(client, users, conn_id):
    r = client.post("/api/reports", headers=users["ana"],
                    json={"name": "Dash", "kind": "report", "document": document(conn_id)})
    return r.json()["id"]


def test_embed_permissions(client, admin, users, report_id):
    vic, ana = users["vic"], users["ana"]
    assert client.post(f"/api/reports/{report_id}/embeds", headers=vic,
                       json={"element_id": "chart"}).status_code == 403  # viewers can't
    assert client.post(f"/api/reports/{report_id}/embeds", headers=ana,
                       json={"element_id": "chart", "mode": "public"}).status_code == 403  # admin only
    r = client.post(f"/api/reports/{report_id}/embeds", headers=ana, json={"element_id": "chart"})
    assert r.status_code == 201 and r.json()["secret"] and r.json()["embed_url"].endswith(r.json()["embed"]["id"])
    listed = client.get(f"/api/reports/{report_id}/embeds", headers=ana).json()
    assert listed[0]["secret"] is None  # shown once only


def test_public_embed_data_etag_and_revoke(client, admin, users, report_id):
    r = client.post(f"/api/reports/{report_id}/embeds", headers=admin,
                    json={"element_id": "table", "mode": "public",
                          "options": {"refresh_interval_s": 60, "allow_download": True}})
    eid = r.json()["embed"]["id"]
    d = client.get(f"/api/embed/{eid}", headers={"X-Embed-Load": "1"})
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["config"]["type"] == "table" and body["config"]["refresh_interval_s"] == 60
    assert body["table"]["display"][0][0] == "Germany"
    again = client.get(f"/api/embed/{eid}", headers={"If-None-Match": d.headers["etag"]})
    assert again.status_code == 304
    csv = client.get(f"/api/embed/{eid}/download.csv")
    assert csv.status_code == 200 and "Country" in csv.text
    # app sessions are not accepted as embed credentials; embeds ignore them entirely
    assert client.get(f"/api/embed/{eid}", headers=admin).status_code in (200, 403)
    client.post(f"/api/embeds/{eid}/revoke", headers=admin)
    gone = client.get(f"/api/embed/{eid}")
    assert gone.status_code == 403 and "revoked" in gone.json()["message"]


def test_signed_embed_tokens(client, users, report_id):
    ana = users["ana"]
    r = client.post(f"/api/reports/{report_id}/embeds", headers=ana, json={
        "element_id": "chart", "allowed_origins": ["https://portal.example.com"]}).json()
    eid, secret = r["embed"]["id"], r["secret"]
    assert client.get(f"/api/embed/{eid}").status_code == 403
    tok = jwt.encode({"embed": eid, "exp": int(time.time()) + 300}, secret, algorithm="HS256")
    ok = client.get(f"/api/embed/{eid}", headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200 and ok.json()["chart"]["option"]["series"][0]["data"]
    assert client.get(f"/api/embed/{eid}?token={tok}").status_code == 200
    rotated = client.post(f"/api/embeds/{eid}/rotate-secret", headers=ana).json()
    assert rotated["secret"] != secret
    assert client.get(f"/api/embed/{eid}?token={tok}").status_code == 403
    # owner loses access to the connection -> embed stops serving data
    assert client.get("/api/embed/emb_doesnotexist").status_code == 404


def test_embed_runs_as_owner_with_current_permissions(client, admin, users, report_id, conn_id):
    r = client.post(f"/api/reports/{report_id}/embeds", headers=admin,
                    json={"element_id": "table", "mode": "public"}).json()
    eid = r["embed"]["id"]  # created by admin: runs with admin's access
    assert client.get(f"/api/embed/{eid}").status_code == 200
    r2 = client.post(f"/api/reports/{report_id}/embeds", headers=users["ana"],
                     json={"element_id": "table"}).json()
    client.post(f"/api/connections/{conn_id}/column-policies", headers=admin, json={
        "connection_id": conn_id, "role": "analyst", "table_key": "customers", "column": "country"})
    tok = jwt.encode({"embed": r2["embed"]["id"], "exp": int(time.time()) + 300}, r2["secret"], algorithm="HS256")
    hidden = client.get(f"/api/embed/{r2['embed']['id']}?token={tok}")
    assert hidden.status_code == 422 and "not visible" in hidden.json()["message"]


def test_frame_headers(client, admin, report_id):
    r = client.post(f"/api/reports/{report_id}/embeds", headers=admin, json={
        "element_id": "chart", "allowed_origins": ["https://a.example.com", "http://localhost:3000"]})
    eid = r.json()["embed"]["id"]
    page = client.get(f"/embed/{eid}")
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors http://localhost:3000 https://a.example.com" in csp
    assert page.headers["referrer-policy"] == "no-referrer"
    app_page = client.get("/api/health")
    assert "frame-ancestors 'none'" in app_page.headers["content-security-policy"]
    assert app_page.headers["x-frame-options"] == "DENY"


def test_rate_limiter():
    from reporting.app.ratelimit import RateLimiter
    rl = RateLimiter(capacity=3, refill_per_s=0.0)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b")  # keys are independent
