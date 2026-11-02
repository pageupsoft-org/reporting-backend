"""Public API of the connections module.

Owns credentials and is the only module that talks to customer databases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd
from pydantic import SecretStr

from reporting.core import (
    CompiledStatement,
    ConnectionId,
    Dialect,
    ExecutionLimits,
    Frozen,
    UserId,
)


class CreateConnection(Frozen):
    name: str
    dialect: Dialect
    connection_string: SecretStr


class UpdateConnection(Frozen):
    name: str | None = None
    connection_string: SecretStr | None = None


class ConnectionInfo(Frozen):
    """Everything about a connection except its secret."""

    id: str
    owner: str
    name: str
    dialect: Dialect
    host: str | None
    port: int | None
    default_database: str | None
    username: str | None
    created_at: datetime
    updated_at: datetime


class CheckStep(Frozen):
    name: str  # dns | tcp | tls | login | database | write_check
    ok: bool
    message: str
    warning: bool = False


class ConnectionTestResult(Frozen):
    ok: bool
    steps: tuple[CheckStep, ...]
    can_write: bool | None = None


@dataclass(frozen=True)
class ColumnDescriptor:
    name: str


@dataclass(frozen=True)
class ResultTable:
    columns: tuple[ColumnDescriptor, ...]
    frame: pd.DataFrame
    truncated: bool
    elapsed_ms: int


class ConnectionService(Protocol):
    def create(self, owner: UserId, req: CreateConnection) -> ConnectionInfo: ...
    def update(self, connection_id: ConnectionId, req: UpdateConnection) -> ConnectionInfo: ...
    def delete(self, connection_id: ConnectionId) -> None: ...
    def list(self, ids: set[ConnectionId] | None = None) -> list[ConnectionInfo]: ...
    def test(self, target: CreateConnection | ConnectionId) -> ConnectionTestResult: ...
    def dialect(self, connection_id: ConnectionId) -> Dialect: ...
    def execute_read(self, connection_id: ConnectionId, stmt: CompiledStatement,
                     limits: ExecutionLimits) -> ResultTable: ...


def create_connection_service(app_db_url: str, master_key_b64: str) -> ConnectionService:
    """Factory used by ``app/wiring.py``; hides the concrete implementation."""
    from reporting.connections._internal.service import SqlConnectionService

    return SqlConnectionService(app_db_url, master_key_b64)


def generate_master_key() -> str:
    from reporting.connections._internal.vault import generate_master_key as _gen

    return _gen()


__all__ = [
    "ColumnDescriptor",
    "ConnectionInfo",
    "ConnectionService",
    "ConnectionTestResult",
    "CreateConnection",
    "ResultTable",
    "CheckStep",
    "UpdateConnection",
    "create_connection_service",
    "generate_master_key",
]
