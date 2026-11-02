"""Local users with bcrypt hashes and HS256 JWT sessions.

OIDC login (Authlib) plugs in by mapping the IdP's verified identity to a local user and
issuing the same JWT session; the rest of the module does not change.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import bcrypt
import jwt
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError

from reporting.auth.api import (
    ROLES,
    Action,
    ColumnPolicy,
    CreateUser,
    ResourceRef,
    Session,
    UserInfo,
)
from reporting.core import (
    AccessContext,
    ConnectionId,
    Issue,
    NotFound,
    PermissionDenied,
    Principal,
    ValidationFailed,
    new_id,
)
from reporting.core.db import create_app_engine, create_tables

ALGORITHM = "HS256"
ISSUER = "reporting"
CLOCK_SKEW_S = 30  # tolerate small clock jumps between issuing and verifying hosts

# Actions every role with access to a connection may perform on it.
READ_ACTIONS = {Action.CONNECTION_READ, Action.QUERY_RUN, Action.EXPORT}
ANALYST_ACTIONS = READ_ACTIONS | {Action.METADATA_REFRESH}
ADMIN_ONLY = {Action.RELATIONSHIP_CREATE, Action.USER_MANAGE, Action.EMBED_PUBLIC}


class LocalAuthService:
    def __init__(self, app_db_url: str, jwt_secret: str, session_ttl_s: int) -> None:
        if len(jwt_secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self._secret = jwt_secret
        self._ttl = session_ttl_s
        self._db = create_app_engine(app_db_url)
        md = MetaData()
        self._users = Table(
            "auth_users", md,
            Column("id", String(32), primary_key=True),
            Column("username", String(150), nullable=False, unique=True),
            Column("password_hash", String(100), nullable=False),
            Column("roles", String(100), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self._connections = Table(
            "auth_connections", md,
            Column("connection_id", String(32), primary_key=True),
            Column("owner", String(32), nullable=False),
        )
        self._grants = Table(
            "auth_connection_grants", md,
            Column("connection_id", String(32), primary_key=True),
            Column("subject", String(64), primary_key=True),  # "user:<id>" or "role:<name>"
        )
        self._policies = Table(
            "auth_column_policies", md,
            Column("id", String(32), primary_key=True),
            Column("connection_id", String(32), nullable=False, index=True),
            Column("role", String(20), nullable=False),
            Column("table_key", String(255), nullable=False),
            Column("column", String(255), nullable=False),
            UniqueConstraint("connection_id", "role", "table_key", "column"),
        )
        create_tables(self._db, md)

    # -- identity -------------------------------------------------------------------------

    def create_user(self, req: CreateUser) -> UserInfo:
        username = req.username.strip().lower()
        issues = []
        if not 3 <= len(username) <= 150 or not username.replace("_", "").replace(".", "") \
                .replace("-", "").replace("@", "").isalnum():
            issues.append(Issue("username", "Use 3–150 letters, digits, '.', '_', '-' or '@'"))
        if len(req.password.get_secret_value()) < 8:
            issues.append(Issue("password", "Password must be at least 8 characters"))
        if not req.roles or any(r not in ROLES for r in req.roles):
            issues.append(Issue("roles", f"Roles must be among {', '.join(ROLES)}"))
        if issues:
            raise ValidationFailed("Invalid user", issues)
        uid = new_id("usr")
        hashed = bcrypt.hashpw(req.password.get_secret_value().encode(), bcrypt.gensalt())
        try:
            with self._db.begin() as conn:
                conn.execute(insert(self._users).values(
                    id=uid, username=username, password_hash=hashed.decode(),
                    roles=",".join(sorted(set(req.roles))), created_at=datetime.now(UTC)))
        except IntegrityError:
            raise ValidationFailed("Username is taken",
                                   [Issue("username", "Username is taken")]) from None
        return UserInfo(id=uid, username=username, roles=tuple(sorted(set(req.roles))))

    def list_users(self) -> list[UserInfo]:
        with self._db.connect() as conn:
            rows = conn.execute(select(self._users).order_by(self._users.c.username)).all()
        return [UserInfo(id=r.id, username=r.username, roles=tuple(r.roles.split(",")))
                for r in rows]

    def login(self, username: str, password: str) -> Session:
        with self._db.connect() as conn:
            row = conn.execute(select(self._users).where(
                self._users.c.username == username.strip().lower())).first()
        # Always run bcrypt so timing does not reveal whether the user exists.
        stored = row.password_hash.encode() if row else bcrypt.hashpw(b"x", bcrypt.gensalt(4))
        if not bcrypt.checkpw(password.encode(), stored) or row is None:
            raise PermissionDenied("Invalid username or password")
        now = int(time.time())
        token = jwt.encode(
            {"sub": row.id, "iss": ISSUER, "iat": now, "exp": now + self._ttl},
            self._secret, algorithm=ALGORITHM)
        user = UserInfo(id=row.id, username=row.username, roles=tuple(row.roles.split(",")))
        return Session(token=token, expires_in=self._ttl, user=user)

    def authenticate(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self._secret, algorithms=[ALGORITHM], issuer=ISSUER,
                                leeway=CLOCK_SKEW_S,
                                # A future "iat" only means the issuing clock ran ahead; "exp"
                                # is what bounds the session.
                                options={"require": ["exp", "sub", "iss"], "verify_iat": False})
        except jwt.ExpiredSignatureError:
            raise PermissionDenied("Session expired; sign in again") from None
        except jwt.PyJWTError:
            raise PermissionDenied("Invalid session token") from None
        with self._db.connect() as conn:
            row = conn.execute(select(self._users).where(
                self._users.c.id == claims["sub"])).first()
        if row is None:
            raise PermissionDenied("User no longer exists")
        return Principal(user_id=row.id, username=row.username, roles=tuple(row.roles.split(",")))

    def principal(self, user_id: str) -> Principal:
        """The current identity of a user (used when embeds run as their owner)."""
        with self._db.connect() as conn:
            row = conn.execute(select(self._users).where(self._users.c.id == user_id)).first()
        if row is None:
            raise PermissionDenied("The owner of this content no longer exists")
        return Principal(user_id=row.id, username=row.username, roles=tuple(row.roles.split(",")))

    # -- authorization --------------------------------------------------------------------

    def authorize(self, principal: Principal, action: Action, resource: ResourceRef) -> None:
        if principal.is_admin:
            return
        if action in ADMIN_ONLY:
            raise PermissionDenied("Only administrators can do this")
        if action == Action.CONNECTION_CREATE:
            if "analyst" not in principal.roles:
                raise PermissionDenied("Only analysts and administrators can add connections")
            return
        if action == Action.EMBED_CREATE:
            if "analyst" not in principal.roles:
                raise PermissionDenied("Only analysts and administrators can create embeds")
            return
        if resource.kind != "connection" or resource.id is None:
            raise PermissionDenied("Not allowed")
        if action in (Action.CONNECTION_UPDATE, Action.CONNECTION_DELETE):
            if self._owner(resource.id) != principal.user_id:
                raise PermissionDenied("Only the owner or an administrator can change this "
                                       "connection")
            return
        if ConnectionId(resource.id) not in self.visible_connections(principal):
            raise PermissionDenied("You do not have access to this connection")
        allowed = ANALYST_ACTIONS if "analyst" in principal.roles else READ_ACTIONS
        if action not in allowed:
            raise PermissionDenied("Your role does not allow this")

    def visible_connections(self, principal: Principal) -> set[ConnectionId]:
        with self._db.connect() as conn:
            if principal.is_admin:
                ids = conn.execute(select(self._connections.c.connection_id)).scalars()
                return {ConnectionId(i) for i in ids}
            owned = conn.execute(select(self._connections.c.connection_id).where(
                self._connections.c.owner == principal.user_id)).scalars().all()
            subjects = [f"user:{principal.user_id}", *(f"role:{r}" for r in principal.roles)]
            granted = conn.execute(select(self._grants.c.connection_id).where(
                self._grants.c.subject.in_(subjects))).scalars().all()
        return {ConnectionId(i) for i in (*owned, *granted)}

    def access_context(self, principal: Principal, connection_id: ConnectionId) -> AccessContext:
        self.authorize(principal, Action.CONNECTION_READ, ResourceRef.connection(connection_id))
        if principal.is_admin:
            return AccessContext.unrestricted(connection_id)
        with self._db.connect() as conn:
            rows = conn.execute(select(self._policies).where(
                self._policies.c.connection_id == connection_id,
                self._policies.c.role.in_(principal.roles))).all()
        # A column is hidden if it is hidden for every role the user holds.
        by_role: dict[str, set[str]] = {r: set() for r in principal.roles}
        for r in rows:
            by_role[r.role].add(f"{r.table_key}.{r.column}")
        hidden = set.intersection(*by_role.values()) if by_role else set()
        return AccessContext(connection_id=connection_id, hidden_columns=frozenset(hidden))

    # -- administration -------------------------------------------------------------------

    def _owner(self, connection_id: str) -> str:
        with self._db.connect() as conn:
            owner = conn.execute(select(self._connections.c.owner).where(
                self._connections.c.connection_id == connection_id)).scalar()
        if owner is None:
            raise NotFound(f"Connection {connection_id} not found")
        return str(owner)

    def register_connection(self, connection_id: str, owner: str) -> None:
        with self._db.begin() as conn:
            conn.execute(insert(self._connections).values(connection_id=connection_id,
                                                          owner=owner))

    def unregister_connection(self, connection_id: str) -> None:
        with self._db.begin() as conn:
            for t in (self._connections, self._grants, self._policies):
                conn.execute(delete(t).where(t.c.connection_id == connection_id))

    def grant(self, connection_id: str, subject: str) -> None:
        kind, _, name = subject.partition(":")
        if kind not in ("user", "role") or not name or (kind == "role" and name not in ROLES):
            raise ValidationFailed("Subject must be 'user:<id>' or 'role:<admin|analyst|viewer>'",
                                   [Issue("subject", "Invalid subject")])
        self._owner(connection_id)
        with self._db.begin() as conn:
            exists = conn.execute(select(self._grants).where(
                self._grants.c.connection_id == connection_id,
                self._grants.c.subject == subject)).first()
            if not exists:
                conn.execute(insert(self._grants).values(connection_id=connection_id,
                                                         subject=subject))

    def add_column_policy(self, policy: ColumnPolicy) -> None:
        self._owner(policy.connection_id)
        try:
            with self._db.begin() as conn:
                conn.execute(insert(self._policies).values(id=new_id("pol"),
                                                           **policy.model_dump()))
        except IntegrityError:
            pass  # already present
