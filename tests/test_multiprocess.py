"""Two service instances on one app database stand in for two server processes."""

import sqlite3

import pytest
from pydantic import SecretStr

from reporting.connections import CreateConnection, UpdateConnection, create_connection_service
from reporting.connections._internal import service as conn_service
from reporting.core import CompiledStatement, Dialect, ExecutionLimits, NotFound
from reporting.metadata import create_metadata_service
from reporting.metadata._internal import service as meta_service
from tests.conftest import make_sales_db


@pytest.fixture(autouse=True)
def no_staleness(monkeypatch):
    monkeypatch.setattr(conn_service, "FRESHNESS_S", 0.0)
    monkeypatch.setattr(meta_service, "FRESHNESS_S", 0.0)


def test_rotated_credentials_reach_other_processes(app_db_url, master_key, tmp_path):
    a = create_connection_service(app_db_url, master_key)
    b = create_connection_service(app_db_url, master_key)
    one = make_sales_db(tmp_path / "one.db")
    two = tmp_path / "two.db"
    sqlite3.connect(two).executescript("CREATE TABLE marker (v TEXT); INSERT INTO marker VALUES ('two');")
    info = a.create("u", CreateConnection(name="x", dialect=Dialect.SQLITE, connection_string=f"sqlite:///{one}"))
    q = CompiledStatement("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 1")
    assert b.execute_read(info.id, q, ExecutionLimits()).frame.iloc[0, 0] == "customers"  # b caches
    a.update(info.id, UpdateConnection(connection_string=SecretStr(f"sqlite:///{two}")))
    assert b.execute_read(info.id, q, ExecutionLimits()).frame.iloc[0, 0] == "marker"
    a.delete(info.id)
    with pytest.raises(NotFound):
        b.execute_read(info.id, q, ExecutionLimits())


def test_snapshot_refresh_reaches_other_processes(app_db_url, master_key, sales_db):
    conns = create_connection_service(app_db_url, master_key)
    info = conns.create("u", CreateConnection(name="s", dialect=Dialect.SQLITE, connection_string=f"sqlite:///{sales_db}"))
    a = create_metadata_service(app_db_url, conns)
    b = create_metadata_service(app_db_url, conns)
    assert a.snapshot(info.id).version == 1 and b.snapshot(info.id).version == 1
    sqlite3.connect(sales_db).executescript("CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    assert a.refresh(info.id) == 2
    snap = b.snapshot(info.id)
    assert snap.version == 2 and snap.table("extra") is not None


def test_concurrent_refresh_does_not_crash(app_db_url, master_key, sales_db):
    conns = create_connection_service(app_db_url, master_key)
    info = conns.create("u", CreateConnection(name="s", dialect=Dialect.SQLITE, connection_string=f"sqlite:///{sales_db}"))
    a = create_metadata_service(app_db_url, conns)
    b = create_metadata_service(app_db_url, conns)
    a.snapshot(info.id)
    b.snapshot(info.id)
    sqlite3.connect(sales_db).executescript("CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    # b computes the same next version as a, but a stores it first
    original = b._load_latest
    stale = b._load_latest(info.id, "main")
    b._load_latest = lambda c, d: stale  # b still believes v1 is latest when it starts
    assert a.refresh(info.id) == 2
    b._load_latest = original
    calls = {"n": 0}

    def first_stale(c, d):
        calls["n"] += 1
        return stale if calls["n"] == 1 else original(c, d)
    b._load_latest = first_stale
    assert b.refresh(info.id) == 2  # IntegrityError handled: adopts the stored version
