"""HTTP hardening: security headers, request ids, body size limit, safe 500s, access log."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reporting.app.config import Settings

log = logging.getLogger("reporting.http")
access = logging.getLogger("reporting.access")

APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class BodySizeLimit:
    """Reject request bodies over ``limit`` bytes, with or without a Content-Length header."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.limit:
            await self._reject(send)
            return
        if length is not None:
            await self.app(scope, receive, send)
            return
        # No Content-Length (chunked): buffer up to the limit, then replay to the app.
        messages: list[Message] = []
        seen = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            seen += len(message.get("body", b""))
            if seen > self.limit:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        body = (b'{"error":"payload_too_large","message":"The request is too large (limit '
                + str(self.limit).encode() + b' bytes)"}')
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def install(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def harden(request: Request,
                     call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        h = response.headers
        h["X-Request-ID"] = request_id
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Embed pages set their own CSP (with frame-ancestors from the embed's allow-list).
        if not path.startswith("/embed/"):
            if path.startswith("/api/docs") or path == "/api/openapi.json":
                h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
            else:
                h.setdefault("Content-Security-Policy", APP_CSP)
            h.setdefault("X-Frame-Options", "DENY")
        access.info("%s %s %s %d %.1fms", request_id, request.method, path,
                    response.status_code, (time.perf_counter() - started) * 1000)
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        log.exception("Unhandled error (request %s) on %s %s", request_id, request.method,
                      request.url.path)
        return JSONResponse(
            {"error": "internal_error", "request_id": request_id,
             "message": f"Something went wrong. Quote request id {request_id} to support."},
            status_code=500, headers={"X-Request-ID": request_id})

    app.add_middleware(BodySizeLimit, limit=settings.max_body_bytes)


def client_ip(request: Request) -> str:
    """Client address (behind a proxy, run uvicorn with --proxy-headers so this is real)."""
    return request.client.host if request.client else "unknown"
