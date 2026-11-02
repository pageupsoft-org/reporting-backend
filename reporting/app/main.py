"""FastAPI application factory. Run with ``uvicorn reporting.app.main:app``."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from reporting.app import errors, security
from reporting.app.config import Settings
from reporting.app.ratelimit import RateLimiter
from reporting.app.routes import auth, connections, embeds, metadata, query, reports
from reporting.app.wiring import build_services


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=os.environ.get("REPORTING_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.production and settings.sqlite_app_db:
        logging.getLogger("reporting").warning(
            "Production with a SQLite app database: fine for one server, but use PostgreSQL "
            "for multiple servers and better concurrency")
    services = build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        services.reports.flush_views()  # persist buffered embed view counts

    app = FastAPI(title="Database Reporting Tool", version="0.1.0", lifespan=lifespan,
                  openapi_url="/api/openapi.json" if settings.api_docs else None,
                  docs_url="/api/docs" if settings.api_docs else None, redoc_url=None)
    app.state.services = services
    errors.install(app)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=["*"], allow_headers=["*"],
                           expose_headers=["Content-Disposition", "X-Truncated"])

    api = APIRouter(prefix="/api")
    for module in (auth, connections, metadata, query, reports, embeds):
        api.include_router(module.router)

    @api.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @api.get("/ready", tags=["system"])
    def ready() -> Response:
        """Readiness: the app database answers (for load balancers / orchestrators)."""
        try:
            services.auth.list_users()
        except Exception:
            logging.getLogger("reporting").exception("Readiness check failed")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.include_router(api)
    app.include_router(embeds.page_router)  # before the SPA catch-all

    # Headers (CSP, HSTS, frame protection), request ids, body limit, safe 500s.
    security.install(app, settings)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    per_minute = settings.login_attempts_per_minute
    app.state.login_limiter = RateLimiter(capacity=per_minute, refill_per_s=per_minute / 60)
    app.state.login_ip_limiter = RateLimiter(capacity=per_minute * 3,
                                             refill_per_s=per_minute * 3 / 60)

    if (settings.serve_frontend and settings.static_dir
            and Path(settings.static_dir, "index.html").exists()):
        _mount_spa(app, Path(settings.static_dir))
    return app


class _ImmutableAssets(StaticFiles):
    """Vite puts a content hash in asset names, so they can be cached for a year."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _mount_spa(app: FastAPI, root: Path) -> None:
    app.mount("/assets", _ImmutableAssets(directory=root / "assets"), name="assets")
    index = root / "index.html"
    revalidate = {"Cache-Control": "no-cache"}  # HTML and embed.js: pick up new releases

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path == "api" or path.startswith("api/"):  # unknown API paths are real 404s
            return JSONResponse({"error": "not_found", "message": "No such API endpoint"},
                                status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root.resolve() in candidate.parents:
            return FileResponse(candidate, headers=revalidate)
        return FileResponse(index, headers=revalidate)


app: FastAPI  # built lazily by __getattr__ below


def __getattr__(name: str) -> FastAPI:
    # Lazily build the module-level app so importing this module has no side effects.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
