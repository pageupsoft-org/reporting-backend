"""Engines for the application's own database (shared by every module's tables)."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError


def create_app_engine(url: str) -> Engine:
    """SQLite: WAL + busy timeout so several server processes can share the file.
    Other databases: health-checked, recycled connections."""
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    for plain in ("postgresql://", "postgres://"):  # bare URLs would pick psycopg2 (not installed)
        if url.startswith(plain):
            url = "postgresql+psycopg://" + url[len(plain):]
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5,
                         max_overflow=10)


SCHEMA_LOCK = 7_262_011  # arbitrary, app-wide advisory-lock key for schema creation


def create_tables(engine: Engine, metadata: MetaData) -> None:
    """Create missing tables safely when several server processes start at once.

    PostgreSQL: serialised with a transaction-scoped advisory lock. Any engine: a duplicate
    created by a racing process is retried (``checkfirst`` then sees the table).
    """
    for attempt in range(5):
        try:
            with engine.begin() as conn:
                if conn.dialect.name == "postgresql":
                    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SCHEMA_LOCK})
                metadata.create_all(conn)
            return
        except (IntegrityError, ProgrammingError, OperationalError):
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))
