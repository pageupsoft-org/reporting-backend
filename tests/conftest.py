import sqlite3
from pathlib import Path

import pytest

from reporting.connections import CreateConnection, create_connection_service, generate_master_key
from reporting.core import Dialect

SALES_DDL = """
CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT, email TEXT,
  region_id INTEGER REFERENCES regions(id)
);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10,2));
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  ordered_at DATETIME NOT NULL,
  shipped_at DATETIME,
  total_amount NUMERIC(12,2) NOT NULL,
  discount NUMERIC(12,2) DEFAULT 0
);
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL
);
CREATE TABLE shipments (id INTEGER PRIMARY KEY, order_id INTEGER, carrier TEXT);
"""

SALES_DATA = """
INSERT INTO regions VALUES (1,'EMEA'),(2,'Americas');
INSERT INTO customers VALUES
 (1,'Acme','Germany','a@acme.de',1),(2,'Globex','USA','g@globex.com',2),
 (3,'Initech','USA','i@initech.com',2),(4,'Umbrella','France','u@umbrella.fr',1);
INSERT INTO products VALUES (1,'Widget',10.00),(2,'Gadget',25.50);
INSERT INTO orders VALUES
 (1,1,'paid','2026-01-05 10:00:00','2026-01-08 09:00:00',100.00,0),
 (2,1,'shipped','2026-02-10 23:30:00','2026-02-11 01:00:00',250.00,10),
 (3,2,'paid','2026-03-01 08:00:00','2026-03-06 08:00:00',75.50,0),
 (4,3,'cancelled','2026-03-15 12:00:00',NULL,40.00,0),
 (5,4,'shipped','2026-04-20 12:00:00','2026-04-22 18:00:00',300.00,25);
INSERT INTO order_items VALUES
 (1,1,1,10,10.00),(2,2,1,5,10.00),(3,2,2,8,25.00),(4,3,2,3,25.17),(5,5,1,30,10.00);
INSERT INTO shipments VALUES (1,1,'DHL'),(2,2,'UPS');
"""


def make_sales_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript(SALES_DDL + SALES_DATA)
    con.commit()
    con.close()
    return path


@pytest.fixture
def sales_db(tmp_path: Path) -> Path:
    return make_sales_db(tmp_path / "sales.db")


@pytest.fixture
def app_db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'app.db'}"


@pytest.fixture
def master_key() -> str:
    return generate_master_key()


@pytest.fixture
def connections(app_db_url, master_key):
    return create_connection_service(app_db_url, master_key)


@pytest.fixture
def sales_conn(connections, sales_db):
    return connections.create(
        "user_1",
        CreateConnection(name="Sales", dialect=Dialect.SQLITE,
                         connection_string=f"sqlite:///{sales_db}"),
    )


@pytest.fixture
def metadata(app_db_url, connections):
    from reporting.metadata import create_metadata_service

    return create_metadata_service(app_db_url, connections)


@pytest.fixture
def query(metadata, connections):
    from reporting.query import create_query_service

    return create_query_service(metadata, connections)
