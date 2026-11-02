"""Per-dialect session setup for read-only, time-limited execution."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from reporting.core import Dialect

Cleanup = Callable[[], None]


def _noop() -> None:
    return None


def begin_read_only(conn: Connection, dialect: Dialect, timeout_s: float) -> Cleanup:
    """Put ``conn`` (already inside a transaction) in read-only mode with a statement timeout.

    Returns a cleanup function to call after the statement finishes. The caller always rolls
    the transaction back, so nothing a statement does can ever be committed.
    """
    ms = max(1, int(timeout_s * 1000))
    if dialect is Dialect.POSTGRESQL:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {ms}"))
        return _noop
    if dialect is Dialect.MYSQL:
        # START TRANSACTION READ ONLY must be the first statement of the transaction.
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {ms}")
        return _noop
    if dialect is Dialect.MSSQL:
        # SQL Server has no read-only transaction mode: rely on a read-only login (the
        # connection test warns when the login can write), lock timeout, a driver-level
        # query timeout and an unconditional rollback.
        conn.exec_driver_sql(f"SET LOCK_TIMEOUT {ms}")
        raw = conn.connection.dbapi_connection
        if raw is not None and hasattr(raw, "timeout"):
            raw.timeout = max(1, int(timeout_s))
        return _noop
    if dialect is Dialect.SQLITE:
        raw = conn.connection.dbapi_connection
        conn.exec_driver_sql("PRAGMA query_only = ON")
        deadline = time.monotonic() + timeout_s
        if isinstance(raw, sqlite3.Connection):
            raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)

        def cleanup() -> None:
            if isinstance(raw, sqlite3.Connection):
                raw.set_progress_handler(None, 0)
            conn.exec_driver_sql("PRAGMA query_only = OFF")

        return cleanup
    raise ValueError(f"unsupported dialect {dialect}")


def is_timeout_error(dialect: Dialect, exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    if dialect is Dialect.POSTGRESQL:
        return "statement timeout" in message or "canceling statement" in message
    if dialect is Dialect.MYSQL:
        return "3024" in message or "maximum statement execution time" in message
    if dialect is Dialect.MSSQL:
        return "hyt00" in message or "timeout expired" in message
    return "interrupted" in message


def write_capability_sql(dialect: Dialect) -> str | None:
    """A query returning one row whose first column is truthy when the login can write."""
    if dialect is Dialect.POSTGRESQL:
        return (
            "SELECT EXISTS (SELECT 1 FROM information_schema.table_privileges "
            "WHERE grantee = current_user AND privilege_type IN ('INSERT','UPDATE','DELETE')) "
            "OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
    if dialect is Dialect.MYSQL:
        return (
            "SELECT COUNT(*) FROM information_schema.USER_PRIVILEGES "
            "WHERE GRANTEE = CONCAT('''', SUBSTRING_INDEX(CURRENT_USER(), '@', 1), '''@''', "
            "SUBSTRING_INDEX(CURRENT_USER(), '@', -1), '''') "
            "AND PRIVILEGE_TYPE IN ('INSERT','UPDATE','DELETE','ALL PRIVILEGES')"
        )
    if dialect is Dialect.MSSQL:
        return (
            "SELECT CASE WHEN IS_ROLEMEMBER('db_owner') = 1 OR IS_ROLEMEMBER('db_datawriter') = 1 "
            "OR IS_SRVROLEMEMBER('sysadmin') = 1 THEN 1 ELSE 0 END"
        )
    return None
