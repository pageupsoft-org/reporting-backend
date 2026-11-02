"""Copy the demo sales data into a PostgreSQL database (for live tests and demos).

Usage:  python scripts/seed_postgres_demo.py postgresql://postgres:pw@localhost:5432
Creates (or recreates) the database ``sales`` on that server from data/demo/sales.db
(run scripts/seed_demo.py first). Adds a ``finance`` schema with a cross-schema foreign key
and PostgreSQL-specific types (timestamptz, uuid, boolean).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]

DDL = """
create table regions (id integer primary key, name text not null);
create table customers (id integer primary key, name text not null, country text not null,
  city text, email text, segment text not null, created_at timestamp not null,
  region_id integer not null references regions(id),
  external_id uuid default gen_random_uuid(), vip boolean default false);
create table categories (id integer primary key, name text not null);
create table products (id integer primary key, name text not null, price numeric(10,2) not null,
  category_id integer not null references categories(id), active boolean not null);
create table orders (id integer primary key, customer_id integer not null references customers(id),
  status text not null, ordered_at timestamptz not null, shipped_at timestamptz,
  total_amount numeric(12,2) not null, discount numeric(12,2) not null default 0);
create table order_items (id integer primary key, order_id integer not null references orders(id),
  product_id integer not null references products(id), quantity integer not null,
  unit_price numeric(10,2) not null);
create table shipments (id integer primary key, order_id integer, carrier text, cost numeric(8,2));
create table employees (id integer primary key, name text not null,
  manager_id integer references employees(id));
create schema finance;
create table finance.invoices (id integer primary key,
  order_id integer not null references public.orders(id), amount numeric(12,2));
"""

TABLES = [
    ("regions", "id,name"),
    ("customers", "id,name,country,city,email,segment,created_at,region_id"),
    ("categories", "id,name"),
    ("products", "id,name,price,category_id,active"),
    ("orders", "id,customer_id,status,ordered_at,shipped_at,total_amount,discount"),
    ("order_items", "id,order_id,product_id,quantity,unit_price"),
    ("shipments", "id,order_id,carrier,cost"),
    ("employees", "id,name,manager_id"),
]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    server = sys.argv[1].rstrip("/")
    source = ROOT / "data" / "demo" / "sales.db"
    if not source.exists():
        sys.exit("Run scripts/seed_demo.py first (needs data/demo/sales.db)")
    with psycopg.connect(f"{server}/postgres", autocommit=True) as c:
        c.execute("drop database if exists sales with (force)")  # demo data: disconnect users
        c.execute("create database sales")
    src = sqlite3.connect(source)
    with psycopg.connect(f"{server}/sales") as c:
        c.execute(DDL)
        for table, cols in TABLES:
            rows = src.execute(f"select {cols} from {table}").fetchall()
            if table == "products":
                rows = [(*r[:4], bool(r[4])) for r in rows]
            if table == "orders":  # stored as UTC
                rows = [(*r[:3], f"{r[3]}+00", f"{r[4]}+00" if r[4] else None, *r[5:])
                        for r in rows]
            marks = ",".join(["%s"] * len(cols.split(",")))
            with c.cursor() as cur:
                cur.executemany(f"insert into {table} ({cols}) values ({marks})", rows)
        c.execute("insert into finance.invoices select id, id, total_amount from orders "
                  "where status in ('paid', 'shipped')")
        c.execute("analyze")
        n = c.execute("select count(*) from orders").fetchone()
    print(f"Seeded {server}/sales ({n[0] if n else 0} orders)")


if __name__ == "__main__":
    main()
