"""Maps ``core.errors`` to HTTP status codes with a uniform JSON body."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from reporting.core import (
    Conflict,
    ConnectionFailed,
    NotFound,
    PermissionDenied,
    QueryTimeout,
    ReportingError,
    StaleSnapshot,
    ValidationFailed,
)

STATUS: list[tuple[type[ReportingError], int]] = [
    (StaleSnapshot, 409),
    (Conflict, 409),
    (ValidationFailed, 422),
    (NotFound, 404),
    (PermissionDenied, 403),
    (ConnectionFailed, 502),
    (QueryTimeout, 504),
]


class Unauthenticated(PermissionDenied):
    """Raised by the HTTP layer when the bearer token is missing or invalid (401)."""


class TooManyAttempts(ReportingError):
    """Rate limit hit (429)."""


def _body(exc: ReportingError, code: str) -> dict[str, object]:
    body: dict[str, object] = {"error": code, "message": exc.message}
    if isinstance(exc, ValidationFailed):
        body["issues"] = [{"path": i.path, "message": i.message, "code": i.code}
                          for i in exc.issues]
    if exc.details:
        body["details"] = exc.details
    return body


def install(app: FastAPI) -> None:
    @app.exception_handler(ReportingError)
    async def _reporting_error(_: Request, exc: ReportingError) -> JSONResponse:
        if isinstance(exc, TooManyAttempts):
            return JSONResponse(_body(exc, "rate_limited"), status_code=429,
                                headers={"Retry-After": "60"})
        if isinstance(exc, Unauthenticated):
            return JSONResponse(_body(exc, "unauthenticated"), status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        for cls, status in STATUS:
            if isinstance(exc, cls):
                code = cls.__name__[0].lower() + "".join(
                    f"_{c.lower()}" if c.isupper() else c for c in cls.__name__[1:])
                return JSONResponse(_body(exc, code), status_code=status)
        return JSONResponse(_body(exc, "error"), status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [{"path": ".".join(str(p) for p in e["loc"][1:]) or str(e["loc"][0]),
                   "message": e["msg"], "code": e["type"]} for e in exc.errors()]
        return JSONResponse({"error": "validation_failed", "message": "Invalid request",
                             "issues": issues}, status_code=422)
