import pytest
from pydantic import ValidationError

from reporting.core import ValidationFailed
from reporting.reports import Element, Layout, Parameter, ReportDocument, resolve_parameters
from tests.test_http_reports import admin, client, conn_id, document, login, users  # noqa: F401

SPEC = {"connection_id": "x"}


def doc(*params):
    return ReportDocument(
        elements=[Element(id="c", type="chart", layout=Layout(x=0, y=0, w=6, h=6), binding={"query": SPEC}),
                  Element(id="t", type="table", layout=Layout(x=6, y=0, w=6, h=6), binding={"query": SPEC}),
                  Element(id="txt", type="text", layout=Layout(x=0, y=6, w=12, h=2), text={"content": "hi"})],
        parameters=list(params))


DATE = Parameter(id="date_range", label="Date range", required=True, default="last_30_days",
                 targets=[{"element_id": "c", "ref": "o.ordered_at"}, {"element_id": "t", "ref": "o.ordered_at"}])
CUSTOMER = Parameter(id="customer", label="Customer", type="equals", locked=True,
                     targets=[{"element_id": "t", "ref": "o.customer_id"}])
STATUS = Parameter(id="status", label="Status", type="one_of", targets=[{"element_id": "c", "ref": "o.status"}])


def test_parameter_model_validation():
    with pytest.raises(ValidationError, match="not a table or chart"):
        doc(Parameter(id="p", label="P", targets=[{"element_id": "txt", "ref": "o.x"}]))
    with pytest.raises(ValidationError, match="unique"):
        doc(DATE, DATE)
    with pytest.raises(ValidationError, match="must be a list"):
        Parameter(id="s", label="S", type="one_of", default="a", targets=[{"element_id": "c", "ref": "o.s"}])
    with pytest.raises(ValidationError):
        Parameter(id="Bad Id", label="x", targets=[{"element_id": "c", "ref": "o.s"}])


def test_resolution_precedence_and_ops():
    d = doc(DATE, CUSTOMER, STATUS)
    assert resolve_parameters(d, "c") == [{"ref": "o.ordered_at", "op": "in_period", "value": "last_30_days"}]
    url = resolve_parameters(d, "c", url_values={"date_range": "this_year", "status": "paid, shipped"})
    assert {"ref": "o.ordered_at", "op": "in_period", "value": "this_year"} in url
    assert {"ref": "o.status", "op": "in", "value": ["paid", "shipped"]} in url
    tok = resolve_parameters(d, "t", url_values={"date_range": "this_year"},
                             token_values={"date_range": "last_7_days", "customer": 42})
    assert tok == [{"ref": "o.ordered_at", "op": "in_period", "value": "last_7_days"},
                   {"ref": "o.customer_id", "op": "eq", "value": "42"}]
    assert resolve_parameters(d, "txt") == []


def test_resolution_errors():
    d = doc(DATE.model_copy(update={"default": None}), CUSTOMER)
    with pytest.raises(ValidationFailed, match="needs the filter 'Date range'"):
        resolve_parameters(d, "c")
    with pytest.raises(ValidationFailed, match="only be set by a signed embed token"):
        resolve_parameters(d, "t", url_values={"date_range": "today", "customer": "7"})
    with pytest.raises(ValidationFailed, match="Unknown filter 'nope'"):
        resolve_parameters(d, "c", url_values={"date_range": "today", "nope": "1"})


def filtered_document(connection_id, **param):
    d = document(connection_id)
    d["parameters"] = [{"id": "date_range", "label": "Date range", "required": True, "default": "last_30_days",
                        "targets": [{"element_id": "chart", "ref": "o.ordered_at"},
                                    {"element_id": "table", "ref": "o.ordered_at"}], **param}]
    return d


def test_save_validates_filters(client, admin, conn_id):  # noqa: F811
    ok = client.post("/api/reports", headers=admin, json={"name": "F", "document": filtered_document(conn_id)})
    assert ok.status_code == 201, ok.text
    assert ok.json()["document"]["parameters"][0]["default"] == "last_30_days"
    for bad, fragment in [({"default": "fortnight"}, "not a date range preset"),
                          ({"targets": [{"element_id": "chart", "ref": "o.status"}]}, "needs a date column"),
                          ({"targets": [{"element_id": "chart", "ref": "o.nope"}]}, "does not exist"),
                          ({"targets": [{"element_id": "chart", "ref": "zz.x"}]}, "Unknown alias")]:
        r = client.post("/api/reports", headers=admin, json={"name": "F", "document": filtered_document(conn_id, **bad)})
        assert r.status_code == 422, bad
        assert fragment in r.json()["message"], r.json()


# -- embeds with report filters -------------------------------------------------------------

import time  # noqa: E402

import jwt  # noqa: E402


def embed_doc(connection_id):
    d = document(connection_id)
    d["elements"][2]["binding"]["query"] = {  # table: orders per status, no fixed filters
        "connection_id": connection_id, "base": {"alias": "o", "table": {"database": "main", "name": "orders"}},
        "columns": [{"id": "status", "kind": "field", "ref": "o.status"},
                    {"id": "n", "kind": "aggregate", "fn": "count"}],
        "order_by": [{"ref": "status"}]}
    d["parameters"] = [
        {"id": "status", "label": "Status", "type": "one_of", "targets": [{"element_id": "table", "ref": "o.status"}]},
        {"id": "customer", "label": "Customer", "type": "equals", "locked": True, "required": True,
         "targets": [{"element_id": "table", "ref": "o.customer_id"}]},
    ]
    return d


def counts(resp):
    assert resp.status_code == 200, resp.text
    return {row[0]: row[1] for row in resp.json()["table"]["rows"]}


def test_embed_filters_url_token_locked_required(client, admin, conn_id):  # noqa: F811
    rid = client.post("/api/reports", headers=admin, json={"name": "F", "document": embed_doc(conn_id)}).json()["id"]
    info = client.post(f"/api/reports/{rid}/embeds", headers=admin, json={"element_id": "table"}).json()
    eid, secret = info["embed"]["id"], info["secret"]
    token = lambda **params: jwt.encode({"embed": eid, "exp": int(time.time()) + 300, "params": params},  # noqa: E731
                                        secret, algorithm="HS256")

    # required locked filter missing -> clear error
    r = client.get(f"/api/embed/{eid}?token={token()}")
    assert r.status_code == 422 and "needs the filter 'Customer'" in r.json()["message"]
    # locked filter from the URL -> refused, even with a valid token
    r = client.get(f"/api/embed/{eid}?token={token()}&p.customer=1")
    assert r.status_code == 422 and "signed embed token" in r.json()["message"]
    # customer 1 (Acme) has orders 1 (paid) and 2 (shipped)
    assert counts(client.get(f"/api/embed/{eid}?token={token(customer=1)}")) == {"paid": 1, "shipped": 1}
    # unlocked filter from the URL narrows further; token values are tamper-proof
    assert counts(client.get(f"/api/embed/{eid}?token={token(customer=1)}&p.status=paid")) == {"paid": 1}
    # config tells the embed page which filters apply
    cfg = client.get(f"/api/embed/{eid}?token={token(customer=2)}").json()["config"]
    assert [f["id"] for f in cfg["filters"]] == ["status", "customer"]
    # values are validated like any filter
    r = client.get(f"/api/embed/{eid}?token={token(customer='abc')}")
    assert r.status_code == 422 and "whole number" in r.json()["message"]
    # unknown filter names are rejected
    assert client.get(f"/api/embed/{eid}?token={token(customer=1)}&p.zzz=1").status_code == 422


def test_public_embed_date_range_default_and_url(client, admin, conn_id):  # noqa: F811
    d = embed_doc(conn_id)
    d["parameters"] = [{"id": "date_range", "label": "Date range", "default": "this_year",
                        "targets": [{"element_id": "table", "ref": "o.ordered_at"}]}]
    rid = client.post("/api/reports", headers=admin, json={"name": "D", "document": d}).json()["id"]
    eid = client.post(f"/api/reports/{rid}/embeds", headers=admin,
                      json={"element_id": "table", "mode": "public"}).json()["embed"]["id"]
    this_year = counts(client.get(f"/api/embed/{eid}"))
    last_year = counts(client.get(f"/api/embed/{eid}?p.date_range=last_year"))
    assert this_year != last_year  # sample orders are in 2026
    bad = client.get(f"/api/embed/{eid}?p.date_range=fortnight")
    assert bad.status_code == 422 and "choose one of" in bad.json()["message"]
