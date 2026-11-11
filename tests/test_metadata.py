import sqlite3

import pandas as pd
import pytest

from reporting.core import DatabaseRef, DataType, Dialect, NotFound, TableRef, ValidationFailed
from reporting.metadata import AddRelationship, create_metadata_service
from reporting.metadata._internal.builder import build_snapshot
from reporting.metadata._internal.types import map_native_type


def ref(conn, name):
    return TableRef(connection_id=conn.id, database="main", name=name)


@pytest.mark.parametrize("dialect,native,expected", [
    (Dialect.POSTGRESQL, "numeric(12,2)", DataType.DECIMAL),
    (Dialect.POSTGRESQL, "timestamp with time zone", DataType.DATETIME_TZ),
    (Dialect.POSTGRESQL, "character varying(200)", DataType.TEXT),
    (Dialect.POSTGRESQL, "integer[]", DataType.OTHER),
    (Dialect.MSSQL, "nvarchar(max)", DataType.TEXT),
    (Dialect.MSSQL, "datetime2", DataType.DATETIME),
    (Dialect.MSSQL, "uniqueidentifier", DataType.UUID),
    (Dialect.MSSQL, "bit", DataType.BOOLEAN),
    (Dialect.MYSQL, "tinyint(1)", DataType.BOOLEAN),
    (Dialect.MYSQL, "int(10) unsigned", DataType.INTEGER),
    (Dialect.MYSQL, "enum('a','b')", DataType.TEXT),
    (Dialect.POSTGRESQL, "enum(work_request_status)", DataType.TEXT),
    (Dialect.POSTGRESQL, 'enum(public."Status")', DataType.TEXT),
    (Dialect.SQLITE, "NUMERIC(12,2)", DataType.DECIMAL),
    (Dialect.SQLITE, "DATETIME", DataType.DATETIME),
    (Dialect.SQLITE, "VARCHAR(10)", DataType.TEXT),
])
def test_type_mapping(dialect, native, expected):
    assert map_native_type(dialect, native) == expected


def test_list_databases_and_tables(metadata, sales_conn):
    assert [d.database for d in metadata.list_databases(sales_conn.id)] == ["main"]
    tables = metadata.list_tables(DatabaseRef(connection_id=sales_conn.id, database="main"))
    names = [t.ref.name for t in tables]
    assert names == ["customers", "order_items", "orders", "products", "regions", "shipments"]
    assert {t.ref.name: t.row_estimate for t in tables}["orders"] == 5


def test_get_table_columns_and_relationships(metadata, sales_conn):
    d = metadata.get_table(ref(sales_conn, "orders"))
    cols = {c.name: c for c in d.columns}
    assert cols["id"].primary_key and not cols["id"].nullable
    assert cols["total_amount"].data_type == DataType.DECIMAL
    assert cols["ordered_at"].data_type == DataType.DATETIME
    assert cols["shipped_at"].nullable
    out = {r.related_table.name: r for r in d.outgoing}
    inc = {r.related_table.name: r for r in d.incoming}
    assert out["customers"].id == "fk_orders_customer_id"
    assert out["customers"].column_pairs == (("customer_id", "id"),)
    assert out["customers"].cardinality == "many_to_one"
    assert inc["order_items"].cardinality == "one_to_many"
    assert inc["shipments"].source == "inferred"  # shipments.order_id has no declared FK


def test_join_paths(metadata, sales_conn):
    paths = metadata.find_join_paths(ref(sales_conn, "orders"), ref(sales_conn, "regions"))
    assert len(paths) == 1
    assert paths[0].tables == ("orders", "customers", "regions")
    assert [s.cardinality for s in paths[0].steps] == ["many_to_one", "many_to_one"]
    back = metadata.find_join_paths(ref(sales_conn, "customers"), ref(sales_conn, "order_items"))
    assert back[0].steps[0].column_pairs == (("id", "customer_id"),)
    assert back[0].steps[0].cardinality == "one_to_many"
    assert metadata.find_join_paths(ref(sales_conn, "regions"), ref(sales_conn, "products"),
                                    max_hops=2) == []
    with pytest.raises(NotFound):
        metadata.find_join_paths(ref(sales_conn, "orders"), ref(sales_conn, "nope"))


def test_multiple_shortest_paths():
    # a -> b -> d and a -> c -> d: both shortest paths are returned.
    tables = pd.DataFrame([{"schema_name": None, "table_name": t, "kind": "table",
                            "row_estimate": None} for t in "abcd"])
    columns = pd.DataFrame(
        [{"schema_name": None, "table_name": t, "column_name": c, "native_type": "INTEGER",
          "nullable": False, "ordinal": i}
         for t, cols in {"a": ["id", "b_ref", "c_ref"], "b": ["id", "d_ref"],
                         "c": ["id", "d_ref"], "d": ["id"]}.items()
         for i, c in enumerate(cols)])
    keys = pd.DataFrame(
        [{"constraint_name": f"pk_{t}", "kind": "p", "schema_name": None, "table_name": t,
          "column_name": "id", "ref_schema": None, "ref_table": None, "ref_column": None,
          "position": 1} for t in "abcd"]
        + [{"constraint_name": n, "kind": "f", "schema_name": None, "table_name": t,
            "column_name": c, "ref_schema": None, "ref_table": rt, "ref_column": "id",
            "position": 1}
           for n, t, c, rt in [("ab", "a", "b_ref", "b"), ("ac", "a", "c_ref", "c"),
                               ("bd", "b", "d_ref", "d"), ("cd", "c", "d_ref", "d")]])
    snap = build_snapshot(connection_id="x", database="main", dialect=Dialect.POSTGRESQL,
                          version=1, tables=tables, columns=columns, keys=keys)
    from reporting.metadata import join_paths
    paths = join_paths(snap, "a", "d")
    assert sorted(p.tables for p in paths) == [("a", "b", "d"), ("a", "c", "d")]


def test_snapshot_versioning_and_diff(metadata, sales_conn, sales_db):
    s1 = metadata.snapshot(sales_conn.id)
    assert s1.version == 1
    assert metadata.refresh(sales_conn.id) == 1  # unchanged structure keeps its version
    con = sqlite3.connect(sales_db)
    con.executescript("ALTER TABLE orders DROP COLUMN discount; "
                      "ALTER TABLE customers ADD COLUMN vip BOOLEAN; DROP TABLE shipments;")
    con.close()
    assert metadata.refresh(sales_conn.id) == 2
    assert metadata.snapshot(sales_conn.id).version == 2
    assert metadata.snapshot(sales_conn.id, version=1).table("orders").column("discount")
    diff = metadata.diff(sales_conn.id, "main", 1, 2)
    assert diff.removed_tables == ("shipments",)
    assert "column orders.discount was removed" in diff.describe()
    assert "column customers.vip was added" in diff.describe()


def test_snapshot_persists_across_instances(app_db_url, connections, sales_conn):
    a = create_metadata_service(app_db_url, connections)
    a.snapshot(sales_conn.id)
    b = create_metadata_service(app_db_url, connections)
    assert b.snapshot(sales_conn.id).version == 1



def test_old_format_snapshot_is_reread(app_db_url, connections, sales_conn):
    a = create_metadata_service(app_db_url, connections)
    a.snapshot(sales_conn.id)
    from sqlalchemy import create_engine, text  # mark the stored snapshot as an older format
    with create_engine(app_db_url).begin() as conn:
        payload = conn.execute(text("SELECT payload FROM metadata_snapshots")).scalar_one()
        conn.execute(text("UPDATE metadata_snapshots SET payload = :p"),
                     {"p": payload.replace('"format":2', '"format":1')})
    b = create_metadata_service(app_db_url, connections)
    snap = b.snapshot(sales_conn.id)
    assert (snap.version, snap.format) == (2, 2)  # re-read and stored as a new version
    c = create_metadata_service(app_db_url, connections)
    assert c.snapshot(sales_conn.id).version == 2  # no further re-reads

def test_user_relationship(metadata, sales_conn):
    db = DatabaseRef(connection_id=sales_conn.id, database="main")
    with pytest.raises(ValidationFailed):
        metadata.add_user_relationship(db, AddRelationship(
            from_table="order_items", to_table="customers", column_pairs=(("nope", "id"),)))
    rel = metadata.add_user_relationship(db, AddRelationship(
        from_table="shipments", to_table="orders", column_pairs=(("order_id", "id"),),
        name="shipment_order"))
    inc = {r.id: r for r in metadata.related_tables(ref(sales_conn, "orders"))
           if r.related_table.name == "shipments"}
    assert set(inc) == {rel.id}  # user relationship supersedes the inferred one
    assert inc[rel.id].source == "user"
