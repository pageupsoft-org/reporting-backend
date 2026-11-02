"""Public API of the auth module: identity, permissions and column policies.

Only the HTTP layer calls ``auth``. ``query`` receives an ``AccessContext`` instead, so it never
calls ``auth`` directly.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import SecretStr

from reporting.core import AccessContext, ConnectionId, Frozen, Principal

ROLES = ("admin", "analyst", "viewer")
RoleName = Literal["admin", "analyst", "viewer"]


class Action(StrEnum):
    CONNECTION_CREATE = "connection.create"
    CONNECTION_READ = "connection.read"
    CONNECTION_UPDATE = "connection.update"
    CONNECTION_DELETE = "connection.delete"
    METADATA_REFRESH = "metadata.refresh"
    RELATIONSHIP_CREATE = "relationship.create"
    QUERY_RUN = "query.run"
    EXPORT = "export"
    USER_MANAGE = "user.manage"
    EMBED_CREATE = "embed.create"
    EMBED_PUBLIC = "embed.public"


class ResourceRef(Frozen):
    kind: Literal["system", "connection"] = "system"
    id: str | None = None

    @classmethod
    def connection(cls, connection_id: str) -> ResourceRef:
        return cls(kind="connection", id=connection_id)


SYSTEM = ResourceRef()


class UserInfo(Frozen):
    id: str
    username: str
    roles: tuple[RoleName, ...]


class CreateUser(Frozen):
    username: str
    password: SecretStr
    roles: tuple[RoleName, ...] = ("viewer",)


class Session(Frozen):
    token: str
    expires_in: int
    user: UserInfo


class ColumnPolicy(Frozen):
    """Hide ``table_key.column`` on a connection from every user holding ``role``."""

    connection_id: str
    role: RoleName
    table_key: str
    column: str


class AuthService(Protocol):
    def authenticate(self, token: str) -> Principal: ...
    def principal(self, user_id: str) -> Principal: ...
    def authorize(self, principal: Principal, action: Action, resource: ResourceRef) -> None: ...
    def visible_connections(self, principal: Principal) -> set[ConnectionId]: ...
    def access_context(self, principal: Principal,
                       connection_id: ConnectionId) -> AccessContext: ...

    # identity and administration
    def login(self, username: str, password: str) -> Session: ...
    def create_user(self, req: CreateUser) -> UserInfo: ...
    def list_users(self) -> list[UserInfo]: ...
    def register_connection(self, connection_id: str, owner: str) -> None: ...
    def unregister_connection(self, connection_id: str) -> None: ...
    def grant(self, connection_id: str, subject: str) -> None: ...
    def add_column_policy(self, policy: ColumnPolicy) -> None: ...


def create_auth_service(app_db_url: str, jwt_secret: str,
                        session_ttl_s: int = 12 * 3600) -> AuthService:
    from reporting.auth._internal.service import LocalAuthService

    return LocalAuthService(app_db_url, jwt_secret, session_ttl_s)


__all__ = [
    "ROLES",
    "SYSTEM",
    "Action",
    "AuthService",
    "ColumnPolicy",
    "CreateUser",
    "ResourceRef",
    "RoleName",
    "Session",
    "UserInfo",
    "create_auth_service",
]
