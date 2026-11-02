import sqlite3

import pytest
from pydantic import SecretStr

from reporting.connections import CreateConnection, UpdateConnection, create_connection_service
from reporting.connections._internal.engines import EnginePool
from reporting.connections._internal.parsing import parse_connection_string
from reporting.core import (
    CompiledStatement,
    ConnectionFailed,
    Dialect,
    ExecutionLimits,
    NotFound,
    QueryTimeout,
    ValidationFailed,
)

# -- parsing ------------------------------------------------------------------------------


def test_parse_postgres_url():
    p = parse_connection_string(
        Dialect.POSTGRESQL, "postgresql://bob:s%40cret@db.local/sales?sslmode=require"
    )
    assert (p.host, p.port, p.database, p.user, p.password) == (
        "db.local", 5432, "sales", "bob", "s@cret")
    assert p.options == {"sslmode": "require"}
    assert p.sqlalchemy_url().drivername == "postgresql+psycopg"


def test_parse_mssql_ado():
    p = parse_connection_string(
        Dialect.MSSQL,
        "Server=tcp:sql.local,14330;Initial Catalog=erp;User Id=sa;Password=x;Encrypt=yes",
    )
    assert (p.host, p.port, p.database, p.user) == ("sql.local", 14330, "erp", "sa")
    assert p.options == {"encrypt": "yes"}
    assert "SQL Server" in p.sqlalchemy_url().query["driver"]


@pytest.mark.parametrize("text,msg", [
    ("mysql://u:p@h/db?local_infile=1", "LOCAL INFILE"),
    ("mysql://u:p@h/db?init_command=DROP", "init commands"),
    ("mysql://u:p@h/db?bogus=1", "not supported"),
    ("postgresql://u:p@h/db", "does not match"),
    ("mysql://u:p@h:99999/db", "Port"),
    ("", "empty"),
])
def test_parse_rejects(text, msg):
    with pytest.raises(ValidationFailed, match=msg):
        parse_connection_string(Dialect.MYSQL, text)


# -- vault and CRUD -----------------------------------------------------------------------


def test_create_never_exposes_secret(connections, app_db_url):
    info = connections.create("u1", CreateConnection(
        name="Prod", dialect=Dialect.POSTGRESQL,
        connection_string="postgresql://bob:TopSecret1@db.local/sales"))
    dumped = info.model_dump_json()
    assert "TopSecret1" not in dumped and info.username == "bob"
    raw = sqlite3.connect(app_db_url.removeprefix("sqlite:///")).execute(
        "SELECT * FROM connections_connections").fetchall()
    assert b"TopSecret1" not in repr(raw).encode()


def test_secret_needs_master_key(app_db_url, master_key, sales_db):
    svc = create_connection_service(app_db_url, master_key)
    info = svc.create("u", CreateConnection(name="s", dialect=Dialect.SQLITE,
                                            connection_string=f"sqlite:///{sales_db}"))
    from reporting.connections import generate_master_key
    other = create_connection_service(app_db_url, generate_master_key())
    with pytest.raises(Exception, match="decrypted"):
        other.execute_read(info.id, CompiledStatement("SELECT 1"), ExecutionLimits())


def test_list_update_delete(connections, sales_conn, sales_db):
    assert [c.id for c in connections.list()] == [sales_conn.id]
    assert connections.list(ids=set()) == []
    renamed = connections.update(sales_conn.id, UpdateConnection(name="Sales EU"))
    assert renamed.name == "Sales EU"
    with pytest.raises(ConnectionFailed):
        connections.update(sales_conn.id, UpdateConnection(
            connection_string=SecretStr("sqlite:////nope/missing.db")))
    connections.delete(sales_conn.id)
    with pytest.raises(NotFound):
        connections.dialect(sales_conn.id)


def test_test_steps(connections, sales_conn):
    result = connections.test(sales_conn.id)
    assert result.ok and result.can_write is True
    bad = connections.test(CreateConnection(name="x", dialect=Dialect.MYSQL,
                                            connection_string="mysql://u@h/db?local_infile=1"))
    assert not bad.ok and bad.steps[0].name == "parse"
    unreachable = connections.test(CreateConnection(
        name="x", dialect=Dialect.POSTGRESQL,
        connection_string="postgresql://u:p@nonexistent.invalid/db"))
    assert not unreachable.ok and unreachable.steps[-1].name == "dns"


# -- execution ----------------------------------------------------------------------------


def test_execute_read_with_params_and_cap(connections, sales_conn):
    r = connections.execute_read(
        sales_conn.id,
        CompiledStatement("SELECT id, total_amount FROM orders WHERE total_amount > :m "
                          "ORDER BY id", {"m": 50}),
        ExecutionLimits(max_rows=2),
    )
    assert list(r.frame["id"]) == [1, 2] and r.truncated
    assert [c.name for c in r.columns] == ["id", "total_amount"]


def test_execute_read_is_read_only(connections, sales_conn, sales_db):
    with pytest.raises(ConnectionFailed, match="readonly|read-only|query_only|attempt to write"):
        connections.execute_read(sales_conn.id, CompiledStatement("DELETE FROM orders"),
                                 ExecutionLimits())
    count = sqlite3.connect(sales_db).execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count == 5


def test_execute_read_timeout(connections, sales_conn):
    slow = ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) "
            "SELECT COUNT(*) FROM n")
    with pytest.raises(QueryTimeout):
        connections.execute_read(sales_conn.id, CompiledStatement(slow),
                                 ExecutionLimits(timeout_s=0.3))


def test_engine_pool_idle_disposal():
    now = [0.0]
    pool = EnginePool(idle_seconds=10, clock=lambda: now[0])

    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    a = FakeEngine()
    assert pool.get("c1", "db", lambda: a) is a
    assert pool.get("c1", "db", lambda: FakeEngine()) is a  # reused
    now[0] = 11
    b = pool.get("c2", "db", lambda: FakeEngine())
    assert a.disposed and len(pool) == 1 and not b.disposed
    pool.dispose("c2")
    assert b.disposed and len(pool) == 0


def test_parse_mssql_named_instance_windows_auth():
    p = parse_connection_string(
        Dialect.MSSQL,
        r"Server=PAGEUPPC\SQLEXPRESS;Database=Restrofit_Master_Dev;Trusted_Connection=True;"
        "TrustServerCertificate=True;Encrypt=False",
    )
    assert (p.host, p.port, p.network_host, p.instance) == (
        r"PAGEUPPC\SQLEXPRESS", None, "PAGEUPPC", "SQLEXPRESS")
    assert p.options == {"trusted_connection": "yes", "trustservercertificate": "yes",
                         "encrypt": "no"}
    assert p.windows_auth and p.user is None
    url = p.sqlalchemy_url()
    assert url.host == r"PAGEUPPC\SQLEXPRESS" and url.port is None


@pytest.mark.parametrize("text,host,port", [
    (r"Server=.\SQLEXPRESS;Integrated Security=SSPI", r"localhost\SQLEXPRESS", None),
    ("Server=(local);Integrated Security=SSPI", "localhost", 1433),
    (r"Server=db\INST,1500;Integrated Security=true", r"db\INST", 1500),
])
def test_parse_mssql_host_forms(text, host, port):
    p = parse_connection_string(Dialect.MSSQL, text)
    assert (p.host, p.port, p.windows_auth) == (host, port, True)


def test_parse_mssql_rejects_mixed_auth_and_bad_boolean():
    with pytest.raises(ValidationFailed, match="either Trusted_Connection"):
        parse_connection_string(Dialect.MSSQL, "Server=x;Trusted_Connection=yes;User Id=sa")
    with pytest.raises(ValidationFailed, match="True or False"):
        parse_connection_string(Dialect.MSSQL, "Server=x;Encrypt=maybe")


def test_missing_sqlite_file_is_an_error_not_a_new_database(connections, tmp_path):
    from reporting.connections import CreateConnection
    missing = tmp_path / "nope" / "gone.db"
    info = connections.create("u", CreateConnection(name="x", dialect=Dialect.SQLITE,
                                                    connection_string=f"sqlite:///{missing}"))
    with pytest.raises(ConnectionFailed, match="unable to open"):
        connections.execute_read(info.id, CompiledStatement("SELECT 1"), ExecutionLimits())
    assert not missing.exists()
