from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.app.errors import TooManyAttempts
from reporting.app.security import client_ip
from reporting.auth import SYSTEM, Action, CreateUser, Session, UserInfo
from reporting.core import Principal

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login", response_model=Session)
def login(body: LoginRequest, request: Request, services: ServicesDep) -> Session:
    ip = client_ip(request)
    user_key = f"{ip}|{body.username.strip().lower()}"
    if not (request.app.state.login_ip_limiter.allow(ip)
            and request.app.state.login_limiter.allow(user_key)):
        raise TooManyAttempts("Too many sign-in attempts; wait a minute and try again")
    return services.auth.login(body.username, body.password)


@router.get("/auth/me", response_model=Principal)
def me(principal: PrincipalDep) -> Principal:
    return principal


@router.get("/users", response_model=list[UserInfo])
def list_users(services: ServicesDep, principal: PrincipalDep) -> list[UserInfo]:
    services.auth.authorize(principal, Action.USER_MANAGE, SYSTEM)
    return services.auth.list_users()


@router.post("/users", response_model=UserInfo, status_code=201)
def create_user(body: CreateUser, services: ServicesDep, principal: PrincipalDep) -> UserInfo:
    services.auth.authorize(principal, Action.USER_MANAGE, SYSTEM)
    return services.auth.create_user(body)
