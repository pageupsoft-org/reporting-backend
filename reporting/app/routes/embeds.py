"""Embeddable components (design doc §2): management API, data API and the embed page."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.app.ratelimit import RateLimiter
from reporting.app.routes.reports import writable
from reporting.app.wiring import Services
from reporting.auth import SYSTEM, Action
from reporting.core import (
    ConnectionId,
    ExecutionLimits,
    NotFound,
    PermissionDenied,
    Principal,
    ReportingError,
)
from reporting.query import QuerySpec, parse_spec
from reporting.render import ChartSpec, TableOptions
from reporting.reports import CreateEmbed, Element, Embed, Report, resolve_parameters

router = APIRouter(tags=["embeds"])
page_router = APIRouter(include_in_schema=False)

DEFAULT_MAX_AGE_S = 300
CHART_ROW_CAP = 10_000
limiter = RateLimiter(capacity=60, refill_per_s=1.0)


class EmbedInfo(BaseModel):
    embed: Embed
    embed_url: str
    script_url: str
    secret: str | None = None  # signed embeds: returned on create and rotate only


def _base_url(services: Services, request: Request) -> str:
    return (services.settings.public_url or str(request.base_url)).rstrip("/")


def _info(services: Services, request: Request, embed: Embed,
          with_secret: bool = False) -> EmbedInfo:
    base = _base_url(services, request)
    return EmbedInfo(
        embed=embed, embed_url=f"{base}/embed/{embed.id}", script_url=f"{base}/embed.js",
        secret=services.reports.embed_secret(embed) if with_secret and embed.mode == "signed"
        else None,
    )


def _embed_writable(services: Services, principal: Principal, embed_id: str) -> Embed:
    embed = services.reports.get_embed(embed_id)
    if not (principal.is_admin or embed.owner_id == principal.user_id):
        raise PermissionDenied("Only the embed's owner or an administrator can change it")
    return embed


# -- management -----------------------------------------------------------------------------


@router.get("/reports/{report_id}/embeds", response_model=list[EmbedInfo])
def list_embeds(report_id: str, request: Request, services: ServicesDep,
                principal: PrincipalDep) -> list[EmbedInfo]:
    writable(services, principal, report_id)
    return [_info(services, request, e) for e in services.reports.list_embeds(report_id)]


@router.post("/reports/{report_id}/embeds", response_model=EmbedInfo, status_code=201)
def create_embed(report_id: str, body: CreateEmbed, request: Request, services: ServicesDep,
                 principal: PrincipalDep) -> EmbedInfo:
    services.auth.authorize(principal, Action.EMBED_CREATE, SYSTEM)
    if body.mode == "public":
        services.auth.authorize(principal, Action.EMBED_PUBLIC, SYSTEM)
    writable(services, principal, report_id)
    embed = services.reports.create_embed(report_id, principal.user_id, body)
    return _info(services, request, embed, with_secret=True)


@router.post("/embeds/{embed_id}/revoke", response_model=EmbedInfo)
def revoke_embed(embed_id: str, request: Request, services: ServicesDep,
                 principal: PrincipalDep) -> EmbedInfo:
    _embed_writable(services, principal, embed_id)
    return _info(services, request, services.reports.revoke_embed(embed_id))


@router.post("/embeds/{embed_id}/rotate-secret", response_model=EmbedInfo)
def rotate_secret(embed_id: str, request: Request, services: ServicesDep,
                  principal: PrincipalDep) -> EmbedInfo:
    _embed_writable(services, principal, embed_id)
    embed = services.reports.rotate_embed_secret(embed_id)
    return _info(services, request, embed, with_secret=True)


# -- public data API ------------------------------------------------------------------------


def _token(authorization: str | None, token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return token


class Resolved(BaseModel):
    embed: Embed
    report: Report
    element: Element
    filters: list[dict[str, Any]]  # from report filters (URL / signed token / defaults)


def _url_params(request: Request) -> dict[str, str]:
    return {k[2:]: v for k, v in request.query_params.items() if k.startswith("p.")}


def _resolve(services: Services, embed_id: str, token: str | None,
             url_values: dict[str, str]) -> Resolved:
    embed = services.reports.get_embed(embed_id)
    token_values = services.reports.verify_embed_token(embed, token)
    try:
        report = services.reports.get(embed.document_id)
    except NotFound:
        raise NotFound("This content is no longer available") from None
    element = report.document.element(embed.element_id)
    if element is None:
        raise NotFound("This content is no longer available")
    filters = resolve_parameters(report.document, element.id, url_values=url_values,
                                 token_values=token_values)
    return Resolved(embed=embed, report=report, element=element, filters=filters)


def _spec(element: Element, filters: list[dict[str, Any]]) -> QuerySpec:
    """The element's stored query plus the report-filter values (validated like any filter)."""
    assert element.binding is not None
    query = element.binding.query
    return parse_spec({**query, "filters": [*query.get("filters", []), *filters]})


def _payload(services: Services, r: Resolved) -> dict[str, Any]:
    embed, element = r.embed, r.element
    config = {
        "id": embed.id, "type": element.type, "title": element.title,
        "theme": embed.options.theme, "refresh_interval_s": embed.options.refresh_interval_s,
        "show_title": embed.options.show_title, "allow_download": embed.options.allow_download,
        "style": element.style.model_dump(),
        "filters": [{"id": p.id, "label": p.label, "type": p.type, "locked": p.locked}
                    for p in r.report.document.parameters
                    if any(t.element_id == element.id for t in p.targets)],
    }
    if element.type == "text":
        return {"config": config, "text": element.text.model_dump() if element.text else None}
    if element.type == "image":
        return {"config": config, "image": element.image.model_dump() if element.image else None}
    assert element.binding is not None
    # Data runs as the embed owner, with the owner's *current* permissions.
    owner = services.auth.principal(embed.owner_id)
    spec = _spec(element, r.filters)
    access = services.auth.access_context(owner, ConnectionId(spec.connection_id))
    refresh = embed.options.refresh_interval_s
    max_age = min(refresh, DEFAULT_MAX_AGE_S) if refresh else DEFAULT_MAX_AGE_S
    if element.type == "table":
        limits = ExecutionLimits(timeout_s=30, max_rows=element.binding.page_size)
        result = services.query.execute(spec, access, limits, max_cache_age_s=max_age)
        table = services.render.table(result, TableOptions(page_size=element.binding.page_size))
        return {"config": config, "table": table.model_dump(mode="json")}
    result = services.query.execute(spec, access,
                                    ExecutionLimits(timeout_s=30, max_rows=CHART_ROW_CAP),
                                    max_cache_age_s=max_age)
    chart_spec = (ChartSpec.model_validate(element.binding.chart) if element.binding.chart
                  else next(iter(services.render.suggest_charts(result)), None))
    if chart_spec is None:
        raise NotFound("This chart has no chart type configured")
    chart = services.render.chart(result, chart_spec)
    return {"config": config, "chart": chart.model_dump(mode="json")}


def _client_key(request: Request, embed_id: str) -> str:
    return f"{embed_id}|{request.client.host if request.client else '?'}"


def _rate_limited() -> JSONResponse:
    return JSONResponse({"error": "rate_limited", "message": "Too many requests; slow down"},
                        status_code=429, headers={"Retry-After": "10"})


@router.get("/embed/{embed_id}")
def embed_data(embed_id: str, request: Request, services: ServicesDep,
               authorization: Annotated[str | None, Header()] = None,
               if_none_match: Annotated[str | None, Header()] = None,
               x_embed_load: Annotated[str | None, Header()] = None,
               token: str | None = None) -> Response:
    """Data for one embedded element. Accepts only embed tokens, never app sessions."""
    if not limiter.allow(_client_key(request, embed_id)):
        return _rate_limited()
    resolved = _resolve(services, embed_id, _token(authorization, token), _url_params(request))
    embed = resolved.embed
    payload = _payload(services, resolved)
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if x_embed_load:
        services.reports.record_view(embed.id, request.client.host if request.client else "?")
    headers = {"ETag": etag, "Cache-Control": "no-cache", "X-Robots-Tag": "noindex"}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/embed/{embed_id}/download.csv")
def embed_download(embed_id: str, request: Request, services: ServicesDep,
                   authorization: Annotated[str | None, Header()] = None,
                   token: str | None = None) -> Response:
    if not limiter.allow(_client_key(request, embed_id)):
        return _rate_limited()
    resolved = _resolve(services, embed_id, _token(authorization, token), _url_params(request))
    embed, element = resolved.embed, resolved.element
    if not embed.options.allow_download or element.binding is None:
        raise PermissionDenied("Downloads are not enabled for this embed")
    owner = services.auth.principal(embed.owner_id)
    spec = _spec(element, resolved.filters)
    access = services.auth.access_context(owner, ConnectionId(spec.connection_id))
    result = services.query.execute(spec, access, ExecutionLimits(timeout_s=60, max_rows=10_000))
    f = services.render.export(result, "csv")
    return Response(f.data, media_type=f.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{f.filename}"'})


# -- embed page -----------------------------------------------------------------------------


def embed_page_headers(services: Services, embed_id: str) -> dict[str, str]:
    try:
        origins = services.reports.get_embed(embed_id).allowed_origins
    except ReportingError:
        origins = ()
    ancestors = " ".join(origins) if origins else "*"
    return {
        "Content-Security-Policy": (
            f"frame-ancestors {ancestors}; default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'"),
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-cache",
    }


@page_router.get("/embed/{embed_id}")
def embed_page(embed_id: str, request: Request) -> Response:
    services: Services = request.app.state.services
    static = services.settings.static_dir
    page = Path(static, "embed.html") if static else None
    headers = embed_page_headers(services, embed_id)
    if page is None or not page.exists():
        return HTMLResponse("<p>The embed page is not built. Run <code>npm run build</code> "
                            "in <code>frontend/</code>.</p>", status_code=503, headers=headers)
    return HTMLResponse(page.read_text(encoding="utf-8"), headers=headers)
