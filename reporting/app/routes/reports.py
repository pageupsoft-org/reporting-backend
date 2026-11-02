"""Report documents and templates (design doc §1)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.app.wiring import Services
from reporting.auth import Action, ResourceRef
from reporting.core import ConnectionId, Issue, PermissionDenied, Principal, ValidationFailed
from reporting.query import PERIODS, parse_spec
from reporting.render import ChartSpec
from reporting.reports import DocumentKind, Report, ReportDocument, ReportSummary, SaveReport

router = APIRouter(tags=["reports"])


def can_read(principal: Principal, report: Report) -> bool:
    return report.kind == "template" or principal.is_admin or report.owner_id == principal.user_id


def readable(services: Services, principal: Principal, report_id: str) -> Report:
    report = services.reports.get(report_id)
    if not can_read(principal, report):
        raise PermissionDenied("You do not have access to this report")
    return report


def writable(services: Services, principal: Principal, report_id: str) -> Report:
    report = services.reports.get(report_id)
    if not (principal.is_admin or report.owner_id == principal.user_id):
        raise PermissionDenied("Only the owner or an administrator can change this report")
    return report


def clean_document(services: Services, principal: Principal,
                   document: ReportDocument) -> ReportDocument:
    """Shape-check every binding through ``query``/``render``; strip snapshot pins.

    Semantic problems (e.g. a column removed later) surface on the element when viewed.
    """
    issues: list[Issue] = []
    elements = []
    for i, el in enumerate(document.elements):
        if el.binding is None:
            elements.append(el)
            continue
        base = f"document.elements[{i}].binding"
        try:
            spec = parse_spec(el.binding.query)
        except ValidationFailed as exc:
            issues += [Issue(f"{base}.query.{x.path}".rstrip("."), x.message) for x in exc.issues]
            continue
        try:
            services.auth.authorize(principal, Action.QUERY_RUN,
                                    ResourceRef.connection(spec.connection_id))
        except PermissionDenied:
            issues.append(Issue(f"{base}.query.connection_id",
                                "You do not have access to this connection"))
            continue
        if el.type == "chart" and el.binding.chart is not None:
            try:
                ChartSpec.model_validate(el.binding.chart)
            except PydanticValidationError as exc:
                issues += [Issue(f"{base}.chart", e["msg"]) for e in exc.errors()]
                continue
        query = spec.model_dump(mode="json", by_alias=True, exclude_none=True,
                                exclude={"snapshot_version"})
        elements.append(el.model_copy(update={
            "binding": el.binding.model_copy(update={"query": query})}))
    if issues:
        raise ValidationFailed(issues[0].message, issues)
    cleaned = document.model_copy(update={"elements": elements})
    check_parameters(services, principal, cleaned)
    return cleaned


def check_parameters(services: Services, principal: Principal, document: ReportDocument) -> None:
    """Each filter's target column must exist in its element's query, be visible to the
    author, and suit the filter type (date ranges need a date column)."""
    issues: list[Issue] = []
    elements = {e.id: e for e in document.elements}
    for i, p in enumerate(document.parameters):
        base = f"document.parameters[{i}]"
        if p.type == "date_range" and p.default is not None and p.default not in PERIODS:
            issues.append(Issue(f"{base}.default", f"'{p.default}' is not a date range preset"))
        for j, t in enumerate(p.targets):
            binding = elements[t.element_id].binding
            assert binding is not None  # guaranteed by ReportDocument validation
            probe = {"ref": t.ref, "op": "in_period", "value": "today"} if p.type == "date_range" \
                else {"ref": t.ref, "op": "is_not_null"}
            query = {**binding.query, "filters": [*binding.query.get("filters", []), probe]}
            spec = parse_spec(query)
            access = services.auth.access_context(principal, ConnectionId(spec.connection_id))
            report = services.query.validate(spec, access)
            problem = next((x for x in report.issues if x.severity == "error"
                            and x.path.startswith(f"filters[{len(query['filters']) - 1}]")),
                           None) or next((x for x in report.issues if x.severity == "error"), None)
            if problem is not None:
                issues.append(Issue(f"{base}.targets[{j}].ref",
                                    f"{p.label} → {t.ref}: {problem.message}"))
    if issues:
        raise ValidationFailed(issues[0].message, issues)


@router.get("/reports", response_model=list[ReportSummary])
def list_reports(services: ServicesDep, principal: PrincipalDep,
                 kind: DocumentKind | None = None) -> list[ReportSummary]:
    items = services.reports.list_documents(kind=kind)
    return [r for r in items if r.kind == "template" or principal.is_admin
            or r.owner_id == principal.user_id]


@router.post("/reports", response_model=Report, status_code=201)
def create_report(body: SaveReport, services: ServicesDep, principal: PrincipalDep) -> Report:
    document = clean_document(services, principal, body.document)
    return services.reports.create(principal.user_id,
                                   body.model_copy(update={"document": document}))


@router.get("/reports/{report_id}", response_model=Report)
def get_report(report_id: str, services: ServicesDep, principal: PrincipalDep) -> Report:
    return readable(services, principal, report_id)


@router.put("/reports/{report_id}", response_model=Report)
def update_report(report_id: str, body: SaveReport, services: ServicesDep,
                  principal: PrincipalDep) -> Report:
    writable(services, principal, report_id)
    document = clean_document(services, principal, body.document)
    return services.reports.update(report_id, body.model_copy(update={"document": document}))


@router.delete("/reports/{report_id}", status_code=204)
def delete_report(report_id: str, services: ServicesDep, principal: PrincipalDep) -> Response:
    writable(services, principal, report_id)
    services.reports.delete(report_id)
    return Response(status_code=204)


class DuplicateRequest(BaseModel):
    kind: Literal["template", "report"]
    name: str | None = Field(default=None, max_length=200)


@router.post("/reports/{report_id}/duplicate", response_model=Report, status_code=201)
def duplicate_report(report_id: str, body: DuplicateRequest, services: ServicesDep,
                     principal: PrincipalDep) -> Report:
    """"Use template" (template → report) and "Save as template" (report → template)."""
    source = readable(services, principal, report_id)
    name = body.name or (source.name if body.kind != source.kind else f"{source.name} (copy)")
    return services.reports.create(
        principal.user_id,
        SaveReport(name=name, description=source.description, kind=body.kind,
                   document=source.document),
        source_id=source.id,
    )
