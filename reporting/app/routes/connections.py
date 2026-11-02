from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.auth import SYSTEM, Action, ColumnPolicy, ResourceRef
from reporting.connections import (
    ConnectionInfo,
    ConnectionTestResult,
    CreateConnection,
    UpdateConnection,
)
from reporting.core import ConnectionId, UserId

router = APIRouter(tags=["connections"])


@router.get("/connections", response_model=list[ConnectionInfo])
def list_connections(services: ServicesDep, principal: PrincipalDep) -> list[ConnectionInfo]:
    return services.connections.list(services.auth.visible_connections(principal))


@router.post("/connections", response_model=ConnectionInfo, status_code=201)
def create_connection(body: CreateConnection, services: ServicesDep,
                      principal: PrincipalDep) -> ConnectionInfo:
    services.auth.authorize(principal, Action.CONNECTION_CREATE, SYSTEM)
    info = services.connections.create(UserId(principal.user_id), body)
    services.auth.register_connection(info.id, principal.user_id)
    return info


@router.post("/connections/test", response_model=ConnectionTestResult)
def test_new_connection(body: CreateConnection, services: ServicesDep,
                        principal: PrincipalDep) -> ConnectionTestResult:
    services.auth.authorize(principal, Action.CONNECTION_CREATE, SYSTEM)
    return services.connections.test(body)


@router.post("/connections/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(connection_id: str, services: ServicesDep,
                    principal: PrincipalDep) -> ConnectionTestResult:
    services.auth.authorize(principal, Action.CONNECTION_READ,
                            ResourceRef.connection(connection_id))
    return services.connections.test(ConnectionId(connection_id))


@router.patch("/connections/{connection_id}", response_model=ConnectionInfo)
def update_connection(connection_id: str, body: UpdateConnection, services: ServicesDep,
                      principal: PrincipalDep) -> ConnectionInfo:
    services.auth.authorize(principal, Action.CONNECTION_UPDATE,
                            ResourceRef.connection(connection_id))
    info = services.connections.update(ConnectionId(connection_id), body)
    if body.connection_string is not None:
        services.metadata.refresh(connection_id)
    return info


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(connection_id: str, services: ServicesDep,
                      principal: PrincipalDep) -> Response:
    services.auth.authorize(principal, Action.CONNECTION_DELETE,
                            ResourceRef.connection(connection_id))
    services.connections.delete(ConnectionId(connection_id))
    services.auth.unregister_connection(connection_id)
    return Response(status_code=204)


class GrantRequest(BaseModel):
    subject: str  # "user:<id>" or "role:<name>"


@router.post("/connections/{connection_id}/grants", status_code=204)
def grant(connection_id: str, body: GrantRequest, services: ServicesDep,
          principal: PrincipalDep) -> Response:
    services.auth.authorize(principal, Action.CONNECTION_UPDATE,
                            ResourceRef.connection(connection_id))
    services.auth.grant(connection_id, body.subject)
    return Response(status_code=204)


@router.post("/connections/{connection_id}/column-policies", status_code=204)
def add_column_policy(connection_id: str, body: ColumnPolicy, services: ServicesDep,
                      principal: PrincipalDep) -> Response:
    services.auth.authorize(principal, Action.CONNECTION_UPDATE,
                            ResourceRef.connection(connection_id))
    services.auth.add_column_policy(body.model_copy(update={"connection_id": connection_id}))
    return Response(status_code=204)
