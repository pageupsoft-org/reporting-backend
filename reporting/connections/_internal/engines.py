"""Lazy engine pool: one SQLAlchemy engine per (connection, database)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from reporting.connections._internal.parsing import ParsedConnection
from reporting.core import Dialect

IDLE_SECONDS = 15 * 60
POOL_SIZE = 5


@dataclass
class _Entry:
    engine: Engine
    last_used: float


def build_engine(parsed: ParsedConnection, database: str | None) -> Engine:
    url = parsed.sqlalchemy_url(database)
    if parsed.dialect is Dialect.SQLITE:
        # Read-only URI: a missing file is an error instead of a new, empty database.
        path = Path(parsed.database or "").resolve()
        uri = f"{path.as_uri()}?mode=ro"
        return create_engine(
            "sqlite://", poolclass=NullPool,
            creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        )
    connect_args: dict[str, object] = {}
    if parsed.dialect is Dialect.POSTGRESQL or parsed.dialect is Dialect.MYSQL:
        connect_args["connect_timeout"] = 10
    return create_engine(
        url, pool_size=POOL_SIZE, max_overflow=0, pool_pre_ping=True, pool_recycle=1800,
        connect_args=connect_args,
    )


class EnginePool:
    def __init__(self, idle_seconds: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()
        self._idle = idle_seconds
        self._clock = clock

    def get(self, connection_id: str, database: str,
            factory: Callable[[], Engine]) -> Engine:
        now = self._clock()
        with self._lock:
            self._sweep(now)
            entry = self._entries.get((connection_id, database))
            if entry is None:
                entry = _Entry(factory(), now)
                self._entries[(connection_id, database)] = entry
            entry.last_used = now
            return entry.engine

    def dispose(self, connection_id: str) -> None:
        with self._lock:
            for key in [k for k in self._entries if k[0] == connection_id]:
                self._entries.pop(key).engine.dispose()

    def _sweep(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.last_used > self._idle:
                entry.engine.dispose()
                del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
