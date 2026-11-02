"""FastAPI dependencies: services and the authenticated principal."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from reporting.app.errors import Unauthenticated
from reporting.app.wiring import Services
from reporting.core import PermissionDenied, Principal

_bearer = HTTPBearer(auto_error=False)


def get_services(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


def get_principal(
    services: Annotated[Services, Depends(get_services)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if creds is None:
        raise Unauthenticated("Sign in to continue")
    try:
        return services.auth.authenticate(creds.credentials)
    except PermissionDenied as exc:
        raise Unauthenticated(exc.message) from None


ServicesDep = Annotated[Services, Depends(get_services)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
