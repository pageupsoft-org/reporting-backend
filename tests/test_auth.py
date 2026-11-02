import time

import jwt
import pytest

from reporting.auth import Action, ColumnPolicy, CreateUser, ResourceRef, create_auth_service
from reporting.core import PermissionDenied, ValidationFailed

SECRET = "s" * 40


@pytest.fixture
def auth(app_db_url):
    return create_auth_service(app_db_url, SECRET)


@pytest.fixture
def users(auth):
    make = lambda name, *roles: auth.create_user(  # noqa: E731
        CreateUser(username=name, password="password1", roles=roles))
    return {"admin": make("admin", "admin"), "ana": make("ana", "analyst"),
            "bob": make("bob", "analyst"), "vic": make("vic", "viewer")}


def principal(auth, name):
    return auth.authenticate(auth.login(name, "password1").token)


def test_login_and_authenticate(auth, users):
    s = auth.login("ANA ", "password1")
    p = auth.authenticate(s.token)
    assert p.username == "ana" and p.roles == ("analyst",)
    with pytest.raises(PermissionDenied):
        auth.login("ana", "wrong-password")
    with pytest.raises(PermissionDenied):
        auth.login("nobody", "password1")


def test_bad_tokens(auth, users):
    with pytest.raises(PermissionDenied, match="Invalid"):
        auth.authenticate("garbage")
    forged = jwt.encode({"sub": users["ana"].id, "iss": "reporting", "exp": time.time() + 60},
                        "x" * 40, algorithm="HS256")
    with pytest.raises(PermissionDenied, match="Invalid"):
        auth.authenticate(forged)
    expired = jwt.encode({"sub": users["ana"].id, "iss": "reporting", "exp": time.time() - 120},
                         SECRET, algorithm="HS256")
    with pytest.raises(PermissionDenied, match="expired"):
        auth.authenticate(expired)


def test_user_validation(auth, users):
    with pytest.raises(ValidationFailed):
        auth.create_user(CreateUser(username="ana", password="password1"))
    with pytest.raises(ValidationFailed) as exc:
        auth.create_user(CreateUser(username="x", password="short"))
    assert {i.path for i in exc.value.issues} == {"username", "password"}


def test_authorization_rules(auth, users):
    admin, ana, bob, vic = (principal(auth, n) for n in ("admin", "ana", "bob", "vic"))
    conn = ResourceRef.connection("conn_1")
    auth.authorize(ana, Action.CONNECTION_CREATE, ResourceRef())
    with pytest.raises(PermissionDenied):
        auth.authorize(vic, Action.CONNECTION_CREATE, ResourceRef())
    auth.register_connection("conn_1", ana.user_id)
    auth.authorize(ana, Action.CONNECTION_DELETE, conn)
    auth.authorize(ana, Action.METADATA_REFRESH, conn)
    with pytest.raises(PermissionDenied):
        auth.authorize(bob, Action.QUERY_RUN, conn)
    auth.grant("conn_1", "role:viewer")
    auth.authorize(vic, Action.QUERY_RUN, conn)
    with pytest.raises(PermissionDenied):
        auth.authorize(vic, Action.METADATA_REFRESH, conn)
    with pytest.raises(PermissionDenied):
        auth.authorize(vic, Action.CONNECTION_DELETE, conn)
    with pytest.raises(PermissionDenied):
        auth.authorize(ana, Action.RELATIONSHIP_CREATE, conn)
    auth.authorize(admin, Action.RELATIONSHIP_CREATE, conn)
    assert auth.visible_connections(bob) == set()
    auth.grant("conn_1", f"user:{bob.user_id}")
    assert auth.visible_connections(bob) == {"conn_1"}
    assert auth.visible_connections(admin) == {"conn_1"}
    auth.unregister_connection("conn_1")
    assert auth.visible_connections(vic) == set()


def test_access_context_column_policies(auth, users):
    ana, vic, admin = principal(auth, "ana"), principal(auth, "vic"), principal(auth, "admin")
    auth.register_connection("conn_1", ana.user_id)
    auth.grant("conn_1", "role:viewer")
    auth.add_column_policy(ColumnPolicy(connection_id="conn_1", role="viewer",
                                        table_key="customers", column="email"))
    ctx = auth.access_context(vic, "conn_1")
    assert not ctx.column_visible("customers", "email") and ctx.column_visible("customers", "name")
    assert auth.access_context(ana, "conn_1").column_visible("customers", "email")
    assert auth.access_context(admin, "conn_1").column_visible("customers", "email")
    with pytest.raises(PermissionDenied):
        auth.access_context(principal(auth, "bob"), "conn_1")
