"""Shared immutable data objects exchanged between modules."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NewType

from pydantic import BaseModel, ConfigDict, Field

ConnectionId = NewType("ConnectionId", str)
UserId = NewType("UserId", str)
SnapshotVersion = NewType("SnapshotVersion", int)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


class Frozen(BaseModel):
    """Base for frozen Pydantic DTOs."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Dialect(StrEnum):
    POSTGRESQL = "postgresql"
    MSSQL = "mssql"
    MYSQL = "mysql"
    SQLITE = "sqlite"  # local development and demos


class DataType(StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    DATETIME_TZ = "datetime_tz"
    UUID = "uuid"
    JSON = "json"
    OTHER = "other"

    @property
    def is_numeric(self) -> bool:
        return self in (DataType.INTEGER, DataType.DECIMAL, DataType.FLOAT)

    @property
    def is_temporal(self) -> bool:
        return self in (DataType.DATE, DataType.DATETIME, DataType.DATETIME_TZ)

    @property
    def is_orderable(self) -> bool:
        return self.is_numeric or self.is_temporal or self in (DataType.TEXT, DataType.UUID)


class DatabaseRef(Frozen):
    connection_id: str
    database: str


class TableRef(Frozen):
    connection_id: str
    database: str
    schema_: str | None = Field(default=None, alias="schema")
    name: str

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @property
    def key(self) -> str:
        """Stable textual key, ``schema.name`` (or ``name`` when there is no schema)."""
        return f"{self.schema_}.{self.name}" if self.schema_ else self.name

    @property
    def db(self) -> DatabaseRef:
        return DatabaseRef(connection_id=self.connection_id, database=self.database)


class Principal(Frozen):
    user_id: str
    username: str
    roles: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


@dataclass(frozen=True)
class CompiledStatement:
    """SQL text with named bind placeholders plus a parameter dict.

    Only ``query`` and ``metadata`` construct it; ``connections`` executes it.
    ``database`` selects which database of a multi-database connection runs it.
    """

    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    database: str | None = None


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_s: float = 30.0
    max_rows: int = 100_000


PREVIEW_LIMITS = ExecutionLimits(timeout_s=15.0, max_rows=100)


@dataclass(frozen=True)
class AccessContext:
    """What a user may see on one connection.

    ``hidden_columns`` holds ``"table_key.column"`` strings; ``allowed_tables`` holds
    table keys, or is ``None`` for unrestricted access.
    """

    connection_id: str
    allowed_tables: frozenset[str] | None = None
    hidden_columns: frozenset[str] = frozenset()

    def table_visible(self, table_key: str) -> bool:
        return self.allowed_tables is None or table_key in self.allowed_tables

    def column_visible(self, table_key: str, column: str) -> bool:
        return self.table_visible(table_key) and f"{table_key}.{column}" not in self.hidden_columns

    @classmethod
    def unrestricted(cls, connection_id: str) -> AccessContext:
        return cls(connection_id=connection_id)
