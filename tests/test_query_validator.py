
import pytest

from reporting.core import AccessContext, StaleSnapshot
from reporting.query import parse_spec
from tests.spec_fixtures import example_spec


def report(query, raw, access=None):
    return query.validate(parse_spec(raw), access)


def errors(rep):
    return {(i.path, i.message) for i in rep.issues if i.severity == "error"}


def test_example_is_valid(query, sales_conn):
    rep = report(query, example_spec(sales_conn.id))
    assert rep.valid, rep.issues
    roles = {c.id: c.role for c in rep.output_columns}
    assert roles == {"country": "dimension", "revenue": "measure", "orders": "measure",
                     "avg_ship": "measure", "aov": "measure"}
    assert rep.snapshot_version == 1


def test_stale_snapshot_returns_diff(query, metadata, sales_conn, sales_db):
    import sqlite3
    metadata.snapshot(sales_conn.id)
    con = sqlite3.connect(sales_db)
    con.execute("ALTER TABLE orders DROP COLUMN discount")
    con.commit()
    metadata.refresh(sales_conn.id)
    with pytest.raises(StaleSnapshot, match="orders.discount was removed"):
        report(query, example_spec(sales_conn.id, snapshot_version=1))


@pytest.mark.parametrize("mutate,path,fragment", [
    (lambda s: s["base"]["table"].update(name="nope"), "base.table", "does not exist"),
    (lambda s: s["joins"][0].update(relationship_id="fk_order_items_product_id"),
     "joins[0]", "does not connect o"),
    (lambda s: s["joins"][0].update(relationship_id="zzz"), "joins[0].relationship_id",
     "does not exist"),
    (lambda s: s["joins"][0].update(alias="o"), "joins[0].alias", "used twice"),
    (lambda s: s["columns"][0].update(ref="x.country"), "columns[0].ref", "Unknown alias"),
    (lambda s: s["columns"][0].update(ref="c.nope"), "columns[0].ref", "does not exist"),
    (lambda s: s["columns"][1].update(arg="c.country"), "columns[1].fn",
     "Sum needs a number; c.country is text"),
    (lambda s: s["columns"][3]["arg"].update(start="o.status"), "columns[3].arg.start",
     "needs a date"),
    (lambda s: s["columns"][4].update(expr="EXEC('x')"), "columns[4].expr",
     "aov: unknown function 'EXEC'"),
    (lambda s: s["columns"][4].update(expr="[nope] + 1"), "columns[4].expr", "unknown column"),
    (lambda s: s["columns"][1].update(id="country"), "columns[1].id", "used twice"),
    (lambda s: s["filters"][0].update(ref="o.total_amount", op="like", value="x"),
     "filters[0].value", "'like' needs a text"),
    (lambda s: s["filters"][1].update(value=["2026-01-01", "not a date"]), "filters[1].value",
     "not a date"),
    (lambda s: s["filters"][0].update(ref="revenue", op="gt", value=1), "filters[0].ref",
     "having"),
    (lambda s: s["having"][0].update(ref="o.id"), "having[0].ref", "column id"),
    (lambda s: s.update(order_by=[{"ref": "o.status"}]), "order_by[0].ref", "not grouped"),
    (lambda s: s.update(group_by=["revenue"]), "group_by[0]", "aggregated"),
    (lambda s: s.update(group_by=[]), "columns[0]", "must be grouped"),
])
def test_reference_type_and_semantic_errors(query, sales_conn, mutate, path, fragment):
    raw = example_spec(sales_conn.id)
    mutate(raw)
    rep = report(query, raw)
    assert not rep.valid
    hits = [m for p, m in errors(rep) if p == path]
    assert hits and fragment in hits[0], errors(rep)


def test_expression_cycles(query, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["columns"] += [
        {"id": "a", "kind": "expression", "expr": "[b] + 1"},
        {"id": "b", "kind": "expression", "expr": "[a] + 1"},
    ]
    rep = report(query, raw)
    msgs = [m for _, m in errors(rep)]
    assert sum("circular" in m for m in msgs) == 2


def test_expression_aggregation_level(query, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["columns"].append({"id": "bad", "kind": "expression", "expr": "SUM(o.total_amount) + o.discount"})
    assert report(query, raw).valid  # automatic grouping also groups by o.discount
    rep = report(query, {**raw, "group_by": ["country"]})
    assert any("o.discount must be inside an aggregate" in m for _, m in errors(rep))
    raw["columns"][-1]["expr"] = "SUM(o.total_amount) / 2 + LENGTH(c.country)"
    raw["columns"][-1]["expr"] = "[revenue] * 2"
    assert report(query, raw).valid


def test_access_context(query, sales_conn):
    access = AccessContext(sales_conn.id, hidden_columns=frozenset({"customers.country"}))
    rep = report(query, example_spec(sales_conn.id), access)
    assert ("columns[0].ref", "c.country is not visible to your role") in errors(rep)
    hidden_table = AccessContext(sales_conn.id, allowed_tables=frozenset({"orders"}))
    rep = report(query, example_spec(sales_conn.id), hidden_table)
    assert any("customers is not visible" in m for _, m in errors(rep))


def test_fan_out_warning(query, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["joins"].append({"alias": "oi", "from_alias": "o",
                         "relationship_id": "fk_order_items_order_id"})
    raw["columns"].append({"id": "qty", "kind": "aggregate", "fn": "sum", "arg": "oi.quantity"})
    rep = report(query, raw)
    assert rep.valid  # warnings do not block
    warns = {i.path: i.message for i in rep.issues if i.severity == "warning"}
    assert "Revenue would be multiplied by joining order_items" in warns["columns[1]"]
    assert "columns[5]" not in warns  # order_items rows are the grain: not multiplied


def test_fan_out_one_side_measure(query, sales_conn):
    # Summing a customer attribute across their orders multiplies it.
    raw = example_spec(sales_conn.id, columns=[
        {"id": "status", "kind": "field", "ref": "o.status"},
        {"id": "cids", "kind": "aggregate", "fn": "sum", "arg": "c.region_id"},
        {"id": "n", "kind": "aggregate", "fn": "count_distinct", "arg": "c.id"},
    ], having=[], order_by=[])
    warns = [i for i in report(query, raw).issues if i.severity == "warning"]
    assert len(warns) == 1 and warns[0].path == "columns[1]"


def test_filter_coercion(query, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["filters"].append({"ref": "o.id", "op": "gt", "value": "abc"})
    assert any("not a whole number" in m for _, m in errors(report(query, raw)))
    raw["filters"][-1] = {"ref": "o.id", "op": "in", "value": ["1", 2]}
    assert report(query, raw).valid
