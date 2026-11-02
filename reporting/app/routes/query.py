from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Response
from pydantic import BaseModel, Field

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.auth import Action, ResourceRef
from reporting.core import (
    AccessContext,
    ConnectionId,
    ExecutionLimits,
    QueryResult,
    ValidationFailed,
)
from reporting.query import QuerySpec, ValidationIssue, ValidationReport, parse_spec
from reporting.render import ChartPayload, ChartSpec, ExportFormat, TableOptions, TablePayload

router = APIRouter(tags=["query"])

RawSpec = Annotated[dict[str, Any], Body()]

RUN_LIMITS = ExecutionLimits(timeout_s=120.0, max_rows=100_000)


def _authorized(services: ServicesDep, principal: PrincipalDep, raw: Any,
                action: Action = Action.QUERY_RUN) -> tuple[QuerySpec, AccessContext]:
    spec = parse_spec(raw)
    services.auth.authorize(principal, action, ResourceRef.connection(spec.connection_id))
    return spec, services.auth.access_context(principal, ConnectionId(spec.connection_id))


class QueryPayload(BaseModel):
    table: TablePayload
    row_count: int
    truncated: bool
    elapsed_ms: int
    cache_hit: bool
    sql: str
    warnings: list[ValidationIssue]
    suggested_charts: list[ChartSpec]


class SqlPayload(BaseModel):
    sql: str
    params: dict[str, str]
    dialect: str
    snapshot_version: int


def _payload(services: ServicesDep, result: QueryResult, opts: TableOptions) -> QueryPayload:
    return QueryPayload(
        table=services.render.table(result, opts),
        row_count=result.row_count,
        truncated=result.truncated,
        elapsed_ms=result.elapsed_ms,
        cache_hit=result.cache_hit,
        sql=result.sql,
        warnings=[ValidationIssue.model_validate(w.model_dump()) for w in result.warnings],
        suggested_charts=services.render.suggest_charts(result),
    )


@router.post("/query/validate", response_model=ValidationReport)
def validate(raw: RawSpec, services: ServicesDep, principal: PrincipalDep) -> ValidationReport:
    try:
        spec, access = _authorized(services, principal, raw)
    except ValidationFailed as exc:  # shape errors are reported, not raised
        if exc.__class__ is not ValidationFailed:
            raise
        return ValidationReport(valid=False, issues=tuple(
            ValidationIssue(path=i.path, message=i.message, code=i.code) for i in exc.issues))
    return services.query.validate(spec, access)


@router.post("/query/sql", response_model=SqlPayload)
def view_sql(raw: RawSpec, services: ServicesDep, principal: PrincipalDep) -> SqlPayload:
    spec, access = _authorized(services, principal, raw)
    compiled = services.query.compile(spec, access)
    return SqlPayload(sql=compiled.statement_sql,
                      params={k: str(v) for k, v in compiled.params.items()},
                      dialect=compiled.dialect, snapshot_version=compiled.snapshot_version)


@router.post("/query/preview", response_model=QueryPayload)
def preview(raw: RawSpec, services: ServicesDep, principal: PrincipalDep) -> QueryPayload:
    spec, access = _authorized(services, principal, raw)
    return _payload(services, services.query.preview(spec, access), TableOptions(page_size=100))


class RunRequest(BaseModel):
    query: dict[str, Any]
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=500, ge=1, le=10_000)
    refresh: bool = False


@router.post("/query/run", response_model=QueryPayload)
def run(body: RunRequest, services: ServicesDep, principal: PrincipalDep) -> QueryPayload:
    spec, access = _authorized(services, principal, body.query)
    result = services.query.execute(spec, access, RUN_LIMITS, use_cache=not body.refresh)
    return _payload(services, result, TableOptions(page=body.page, page_size=body.page_size))


class ChartRequest(BaseModel):
    query: dict[str, Any]
    chart: ChartSpec


@router.post("/render/chart", response_model=ChartPayload)
def chart(body: ChartRequest, services: ServicesDep, principal: PrincipalDep) -> ChartPayload:
    spec, access = _authorized(services, principal, body.query)
    result = services.query.execute(spec, access, RUN_LIMITS)
    return services.render.chart(result, body.chart)


class ExportRequest(BaseModel):
    query: dict[str, Any]
    format: ExportFormat = "csv"


@router.post("/render/export")
def export(body: ExportRequest, services: ServicesDep, principal: PrincipalDep) -> Response:
    spec, access = _authorized(services, principal, body.query, Action.EXPORT)
    result = services.query.execute(spec, access, RUN_LIMITS)
    f = services.render.export(result, body.format)
    headers = {"Content-Disposition": f'attachment; filename="{f.filename}"'}
    if result.truncated:
        headers["X-Truncated"] = "true"
    return Response(content=f.data, media_type=f.content_type, headers=headers)
