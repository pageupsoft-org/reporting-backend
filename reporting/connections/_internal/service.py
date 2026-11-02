"""SQLAlchemy-backed implementation of ``ConnectionService``."""

from __future__ import annotations

import os
import socket
import ssl
import time
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from reporting.connections._internal.dialects import (
    begin_read_only,
    is_timeout_error,
    write_capability_sql,
)
from reporting.connections._internal.engines import EnginePool, build_engine
from reporting.connections._internal.parsing import (
    ParsedConnection,
    mssql_driver,
    parse_connection_string,
)
from reporting.connections._internal.vault import Sealed, Vault, load_master_key
from reporting.connections.api import (
    CheckStep,
    ColumnDescriptor,
    ConnectionInfo,
    ConnectionTestResult,
    CreateConnection,
    ResultTable,
    UpdateConnection,
)
from reporting.core import (
    CompiledStatement,
    ConnectionFailed,
    ConnectionId,
    Dialect,
    ExecutionLimits,
    NotFound,
    QueryTimeout,
    UserId,
    ValidationFailed,
    new_id,
)
from reporting.core.db import create_app_engine, create_tables

TENANT = "default"
FRESHNESS_S = 2.0  # how stale a process's cached credentials may be


def _now() -> datetime:
    return datetime.now(UTC)


class SqlConnectionService:
    def __init__(self, app_db_url: str, master_key_b64: str,
                 pool: EnginePool | None = None) -> None:
        self._app: Engine = create_app_engine(app_db_url)
        md = MetaData()
        self._vault = Vault(self._app, md, load_master_key(master_key_b64))
        self._t = Table(
            "connections_connections",
            md,
            Column("id", String(32), primary_key=True),
            Column("owner", String(64), nullable=False),
            Column("name", String(200), nullable=False),
            Column("dialect", String(16), nullable=False),
            Column("host", String(255)),
            Column("port", Integer),
            Column("default_database", String(255)),
            Column("username", String(255)),
            Column("secret_nonce", LargeBinary, nullable=False),
            Column("secret_ciphertext", LargeBinary, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        create_tables(self._app, md)
        self._pool = pool or EnginePool()
        self._parsed_cache: dict[str, tuple[ParsedConnection, datetime, float]] = {}

    # -- CRUD -----------------------------------------------------------------------------

    def create(self, owner: UserId, req: CreateConnection) -> ConnectionInfo:
        parsed = parse_connection_string(req.dialect, req.connection_string.get_secret_value())
        cid = new_id("conn")
        sealed = self._vault.encrypt(TENANT, cid, req.connection_string.get_secret_value())
        now = _now()
        with self._app.begin() as conn:
            conn.execute(insert(self._t).values(
                id=cid, owner=owner, name=req.name.strip() or cid, dialect=req.dialect.value,
                host=parsed.host, port=parsed.port, default_database=parsed.database,
                username=parsed.user, secret_nonce=sealed.nonce,
                secret_ciphertext=sealed.ciphertext, created_at=now, updated_at=now,
            ))
        return self._info(cid)

    def update(self, connection_id: ConnectionId, req: UpdateConnection) -> ConnectionInfo:
        current = self._info(connection_id)
        values: dict[str, object] = {"updated_at": _now()}
        if req.name is not None:
            values["name"] = req.name.strip() or current.name
        if req.connection_string is not None:
            secret = req.connection_string.get_secret_value()
            candidate = CreateConnection(name=current.name, dialect=current.dialect,
                                         connection_string=req.connection_string)
            result = self.test(candidate)
            if not result.ok:
                failed = next(s for s in result.steps if not s.ok)
                raise ConnectionFailed(
                    f"New credentials failed the {failed.name} check: {failed.message}"
                )
            parsed = parse_connection_string(current.dialect, secret)
            sealed = self._vault.encrypt(TENANT, connection_id, secret)
            values.update(
                host=parsed.host, port=parsed.port, default_database=parsed.database,
                username=parsed.user, secret_nonce=sealed.nonce,
                secret_ciphertext=sealed.ciphertext,
            )
        with self._app.begin() as conn:
            conn.execute(update(self._t).where(self._t.c.id == connection_id).values(**values))
        if req.connection_string is not None:
            self._pool.dispose(connection_id)
            self._parsed_cache.pop(connection_id, None)
        return self._info(connection_id)

    def delete(self, connection_id: ConnectionId) -> None:
        self._info(connection_id)
        with self._app.begin() as conn:
            conn.execute(delete(self._t).where(self._t.c.id == connection_id))
        self._pool.dispose(connection_id)
        self._parsed_cache.pop(connection_id, None)

    def list(self, ids: set[ConnectionId] | None = None) -> list[ConnectionInfo]:
        stmt = select(self._t).order_by(self._t.c.name)
        if ids is not None:
            stmt = stmt.where(self._t.c.id.in_(sorted(ids)))
        with self._app.connect() as conn:
            return [self._row_to_info(r) for r in conn.execute(stmt)]

    def dialect(self, connection_id: ConnectionId) -> Dialect:
        return self._info(connection_id).dialect

    # -- testing --------------------------------------------------------------------------

    def test(self, target: CreateConnection | ConnectionId) -> ConnectionTestResult:
        if isinstance(target, CreateConnection):
            try:
                parsed = parse_connection_string(
                    target.dialect, target.connection_string.get_secret_value()
                )
            except ValidationFailed as exc:
                step = CheckStep(name="parse", ok=False, message=exc.message)
                return ConnectionTestResult(ok=False, steps=(step,))
        else:
            parsed = self._parsed(target)
        steps = _run_checks(parsed)
        ok = all(s.ok for s in steps)
        can_write = next(
            (s.warning for s in steps if s.name == "write_check"), None
        )
        return ConnectionTestResult(ok=ok, steps=tuple(steps), can_write=can_write)

    # -- execution ------------------------------------------------------------------------

    def execute_read(self, connection_id: ConnectionId, stmt: CompiledStatement,
                     limits: ExecutionLimits) -> ResultTable:
        parsed = self._parsed(connection_id)
        database = stmt.database or parsed.database or "main"
        if parsed.dialect is Dialect.SQLITE:
            database = parsed.database or "main"
        try:
            engine = self._pool.get(
                connection_id, database, lambda: build_engine(parsed, database)
            )
        except ImportError as exc:
            raise ConnectionFailed(
                f"Database driver is not installed ({exc.name}); run `uv sync`") from None
        started = time.perf_counter()
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    cleanup = begin_read_only(conn, parsed.dialect, limits.timeout_s)
                    try:
                        result = conn.execute(text(stmt.sql), stmt.params)
                        keys = list(result.keys())
                        rows = result.fetchmany(limits.max_rows + 1)
                    finally:
                        cleanup()
                finally:
                    trans.rollback()
        except DBAPIError as exc:
            if is_timeout_error(parsed.dialect, exc):
                raise QueryTimeout(
                    f"Query exceeded the {limits.timeout_s:g}s time limit"
                ) from None
            raise ConnectionFailed(_db_message(exc)) from None
        except SQLAlchemyError as exc:
            raise ConnectionFailed(_db_message(exc)) from None
        truncated = len(rows) > limits.max_rows
        rows = rows[: limits.max_rows]
        frame = pd.DataFrame.from_records([tuple(r) for r in rows], columns=keys)
        return ResultTable(
            columns=tuple(ColumnDescriptor(k) for k in keys),
            frame=frame,
            truncated=truncated,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    # -- helpers --------------------------------------------------------------------------

    def _row(self, connection_id: str):  # type: ignore[no-untyped-def]
        with self._app.connect() as conn:
            row = conn.execute(select(self._t).where(self._t.c.id == connection_id)).first()
        if row is None:
            raise NotFound(f"Connection {connection_id} not found")
        return row

    def _info(self, connection_id: str) -> ConnectionInfo:
        return self._row_to_info(self._row(connection_id))

    @staticmethod
    def _row_to_info(row) -> ConnectionInfo:  # type: ignore[no-untyped-def]
        return ConnectionInfo(
            id=row.id, owner=row.owner, name=row.name, dialect=Dialect(row.dialect),
            host=row.host, port=row.port, default_database=row.default_database,
            username=row.username, created_at=row.created_at, updated_at=row.updated_at,
        )

    def _parsed(self, connection_id: str) -> ParsedConnection:
        """Decrypted connection details, cached per process.

        Another server process may rotate credentials or delete the connection, so the row's
        ``updated_at`` is re-checked at most every FRESHNESS_S seconds; a change replaces the
        cached details and disposes this process's pooled engines.
        """
        now = time.monotonic()
        cached = self._parsed_cache.get(connection_id)
        if cached is not None and now - cached[2] <= FRESHNESS_S:
            return cached[0]
        if cached is not None:
            with self._app.connect() as conn:
                stamp = conn.execute(select(self._t.c.updated_at)
                                     .where(self._t.c.id == connection_id)).scalar()
            if stamp is None:
                self._parsed_cache.pop(connection_id, None)
                self._pool.dispose(connection_id)
                raise NotFound(f"Connection {connection_id} not found")
            if stamp == cached[1]:
                self._parsed_cache[connection_id] = (cached[0], cached[1], now)
                return cached[0]
            self._pool.dispose(connection_id)  # credentials changed elsewhere
        row = self._row(connection_id)
        secret = self._vault.decrypt(
            TENANT, connection_id, Sealed(row.secret_nonce, row.secret_ciphertext)
        )
        parsed = parse_connection_string(Dialect(row.dialect), secret)
        self._parsed_cache[connection_id] = (parsed, row.updated_at, now)
        return parsed


def _db_message(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None) or exc
    message = str(orig).strip().splitlines()[0] if str(orig).strip() else type(orig).__name__
    return message[:500]


def _run_checks(parsed: ParsedConnection) -> list[CheckStep]:
    steps: list[CheckStep] = []
    if parsed.dialect is Dialect.SQLITE:
        path = parsed.database or ""
        if not os.path.exists(path):
            return [CheckStep(name="database", ok=False, message=f"File not found: {path}")]
        steps.append(CheckStep(name="database", ok=True, message=f"Found {path}"))
        writable = os.access(path, os.W_OK)
        steps.append(CheckStep(
            name="write_check", ok=True, warning=writable,
            message="File is writable by the service; queries still run with query_only"
            if writable else "File is read-only",
        ))
        return steps

    host, port = parsed.network_host or "", parsed.port or 0
    try:
        addresses = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
        steps.append(CheckStep(name="dns", ok=True, message=f"{host} resolved"))
    except OSError as exc:
        return [CheckStep(name="dns", ok=False, message=f"Cannot resolve {host}: {exc}")]
    if parsed.instance and not parsed.port:
        steps.append(CheckStep(
            name="tcp", ok=True,
            message=f"Named instance {parsed.instance}: its port is found via SQL Server Browser",
        ))
    else:
        tcp = _tcp_check(addresses, host, port)
        steps.append(tcp)
        if not tcp.ok:
            return steps
    steps.extend(_login_checks(parsed))
    return steps


def _tcp_check(addresses: list, host: str, port: int) -> CheckStep:  # type: ignore[type-arg]
    try:
        family, _, _, _, addr = addresses[0]
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(addr)
        return CheckStep(name="tcp", ok=True, message=f"Port {port} is reachable")
    except OSError as exc:
        return CheckStep(name="tcp", ok=False, message=f"Cannot reach {host}:{port}: {exc}")


def _login_checks(parsed: ParsedConnection) -> list[CheckStep]:
    steps: list[CheckStep] = []
    o = parsed.options
    tls_requested = (
        "ssl_ca" in o
        or o.get("sslmode", "disable") not in ("disable", "allow", "prefer")
        or o.get("encrypt") in ("yes", "mandatory", "strict")
        or (parsed.dialect is Dialect.MSSQL and "encrypt" not in o
            and mssql_driver().startswith("ODBC Driver 18"))
    )
    steps.append(CheckStep(
        name="tls", ok=True, warning=not tls_requested,
        message=f"TLS negotiated by the driver (OpenSSL {ssl.OPENSSL_VERSION_INFO[:3]})"
        if tls_requested else "TLS not requested; data travels unencrypted",
    ))
    engine = None
    try:
        engine = build_engine(parsed, parsed.database)
        with engine.connect() as conn:
            who = "your Windows account" if parsed.windows_auth else parsed.user
            steps.append(CheckStep(name="login", ok=True, message=f"Logged in as {who}"))
            conn.execute(text("SELECT 1"))
            steps.append(CheckStep(
                name="database", ok=True,
                message=f"Database {parsed.database or '(default)'} is accessible",
            ))
            sql = write_capability_sql(parsed.dialect)
            if sql:
                can_write = bool(conn.execute(text(sql)).scalar())
                steps.append(CheckStep(
                    name="write_check", ok=True, warning=can_write,
                    message="Login can write data; a read-only login is recommended"
                    if can_write else "Login is read-only",
                ))
    except ImportError as exc:
        steps.append(CheckStep(name="login", ok=False,
                               message=f"Database driver is not installed ({exc.name}); "
                                       "run `uv sync` to install it"))
    except SQLAlchemyError as exc:
        name = "login" if not any(s.name == "login" for s in steps) else "database"
        steps.append(CheckStep(name=name, ok=False, message=_db_message(exc)))
    finally:
        if engine is not None:
            engine.dispose()
    return steps
