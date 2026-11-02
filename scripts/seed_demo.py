"""Create a demo "sales" SQLite database and register it with the app.

Usage:  uv run python scripts/seed_demo.py [--reset]

Creates (under REPORTING_DATA_DIR, default ./data):
  demo/sales.db        regions, customers, products, orders, order_items, shipments
  users                admin (REPORTING_ADMIN_PASSWORD or "admin123"), analyst, viewer
  connection "Sales demo", shared with the viewer role, customers.email hidden from viewers
"""

from __future__ import annotations

import argparse
import contextlib
import os
import random
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DDL = """
CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL, city TEXT,
  email TEXT, segment TEXT NOT NULL, created_at DATETIME NOT NULL,
  region_id INTEGER NOT NULL REFERENCES regions(id)
);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10,2) NOT NULL,
  category_id INTEGER NOT NULL REFERENCES categories(id), active BOOLEAN NOT NULL
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  ordered_at DATETIME NOT NULL,
  shipped_at DATETIME,
  total_amount NUMERIC(12,2) NOT NULL,
  discount NUMERIC(12,2) NOT NULL DEFAULT 0
);
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL
);
-- No declared FK: the tool infers shipments.order_id -> orders.id from the name.
CREATE TABLE shipments (id INTEGER PRIMARY KEY, order_id INTEGER, carrier TEXT, cost NUMERIC(8,2));
CREATE TABLE employees (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  manager_id INTEGER REFERENCES employees(id)  -- self-reference
);
"""

REGIONS = ["EMEA", "Americas", "APAC"]
COUNTRIES = {
    "EMEA": [("Germany", ["Berlin", "Munich"]), ("France", ["Paris", "Lyon"]),
             ("United Kingdom", ["London", "Leeds"]), ("Spain", ["Madrid"])],
    "Americas": [("USA", ["New York", "Austin", "Seattle"]), ("Canada", ["Toronto"]),
                 ("Brazil", ["São Paulo"])],
    "APAC": [("India", ["Pune", "Bengaluru"]), ("Japan", ["Tokyo"]), ("Australia", ["Sydney"])],
}
CATEGORIES = ["Hardware", "Software", "Services", "Accessories"]
PRODUCTS = [("Widget", 10, 0), ("Gadget", 25.5, 0), ("Sprocket", 4.25, 0), ("Server", 1999, 0),
            ("Analytics Suite", 499, 1), ("Backup Pro", 89, 1), ("Install", 150, 2),
            ("Support Plan", 1200, 2), ("Cable", 7.99, 3), ("Dock", 129, 3), ("Keyboard", 49, 3)]
SEGMENTS = ["Enterprise", "SMB", "Consumer"]
COMPANIES = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Wonka",
             "Tyrell", "Cyberdyne"]
STATUSES = ["paid"] * 5 + ["shipped"] * 8 + ["cancelled", "refunded"]


def build_sales_db(path: Path, seed: int = 7) -> None:
    rnd = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(DDL)
    con.executemany("INSERT INTO regions VALUES (?,?)", list(enumerate(REGIONS, 1)))
    con.executemany("INSERT INTO categories VALUES (?,?)", list(enumerate(CATEGORIES, 1)))
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?)", [
        (i, n, p, c + 1, i != 3) for i, (n, p, c) in enumerate(PRODUCTS, 1)])
    start = datetime(2025, 1, 1)
    customers = []
    for cid in range(1, 121):
        region_id = rnd.randint(1, 3)
        country, cities = rnd.choice(COUNTRIES[REGIONS[region_id - 1]])
        name = f"{rnd.choice(COMPANIES)} {cid}"
        customers.append((cid, name, country, rnd.choice(cities),
                          f"contact{cid}@example.com", rnd.choice(SEGMENTS),
                          (start + timedelta(days=rnd.randint(0, 200))).isoformat(sep=" "),
                          region_id))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)
    iid = sid = 0
    for oid in range(1, 2001):
        cust = rnd.choice(customers)
        ordered = start + timedelta(days=rnd.randint(0, 660), hours=rnd.randint(7, 22),
                                    minutes=rnd.randint(0, 59))
        status = rnd.choice(STATUSES)
        shipped = (ordered + timedelta(days=rnd.choice([0, 1, 1, 2, 2, 3, 5, 8]),
                                       hours=rnd.randint(0, 12))
                   if status in ("shipped", "refunded") else None)
        lines, total = [], 0.0
        for _ in range(rnd.choice([1, 1, 2, 3, 4])):
            iid += 1
            pid = rnd.randint(1, len(PRODUCTS))
            qty = rnd.randint(1, 12 if PRODUCTS[pid - 1][1] < 50 else 3)
            price = PRODUCTS[pid - 1][1]
            lines.append((iid, oid, pid, qty, price))
            total += qty * price
        discount = round(total * rnd.choice([0, 0, 0, 0.05, 0.1]), 2)
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", (
            oid, cust[0], status, ordered.isoformat(sep=" ", timespec="seconds"),
            shipped.isoformat(sep=" ", timespec="seconds") if shipped else None,
            round(total - discount, 2), discount))
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", lines)
        if shipped:
            sid += 1
            con.execute("INSERT INTO shipments VALUES (?,?,?,?)", (
                sid, oid, rnd.choice(["DHL", "UPS", "FedEx"]), round(rnd.uniform(5, 60), 2)))
    con.executemany("INSERT INTO employees VALUES (?,?,?)", [
        (1, "Ada", None), (2, "Grace", 1), (3, "Linus", 1), (4, "Ken", 2)])
    con.commit()
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the data dir first")
    args = parser.parse_args()

    data = Path(os.environ.get("REPORTING_DATA_DIR", "data")).resolve()
    if args.reset and data.exists():
        shutil.rmtree(data)
    os.environ.setdefault("REPORTING_ADMIN_PASSWORD", "admin123")

    from pydantic import SecretStr

    from reporting.app.config import Settings
    from reporting.app.wiring import build_services
    from reporting.auth import ColumnPolicy, CreateUser
    from reporting.connections import CreateConnection, UpdateConnection
    from reporting.core import Dialect, ValidationFailed

    sales = data / "demo" / "sales.db"
    build_sales_db(sales)
    services = build_services(Settings.from_env(data))
    admin = next(u for u in services.auth.list_users() if "admin" in u.roles)
    for name, role in (("analyst", "analyst"), ("viewer", "viewer")):
        with contextlib.suppress(ValidationFailed):  # already created on an earlier run
            services.auth.create_user(CreateUser(username=name, password=f"{name}123",
                                                 roles=(role,)))
    existing = [c for c in services.connections.list() if c.name == "Sales demo"]
    if existing:
        # Re-point at this machine's file: data seeded under WSL holds /mnt/d/... paths that
        # do not exist on Windows (and vice versa).
        info = services.connections.update(info_id := existing[0].id, UpdateConnection(
            connection_string=SecretStr(f"sqlite:///{sales}")))
        services.metadata.refresh(info_id)
    else:
        info = services.connections.create(admin.id, CreateConnection(
            name="Sales demo", dialect=Dialect.SQLITE, connection_string=f"sqlite:///{sales}"))
        services.auth.register_connection(info.id, admin.id)
        services.auth.grant(info.id, "role:viewer")
        services.auth.grant(info.id, "role:analyst")
        services.auth.add_column_policy(ColumnPolicy(
            connection_id=info.id, role="viewer", table_key="customers", column="email"))
    snap = services.metadata.snapshot(info.id)
    print(f"Demo ready: connection {info.id} ({len(snap.tables)} tables, "
          f"{len(snap.relationships)} relationships, snapshot v{snap.version})")
    print("Users: admin / " + os.environ["REPORTING_ADMIN_PASSWORD"]
          + ", analyst / analyst123, viewer / viewer123")


if __name__ == "__main__":
    main()
