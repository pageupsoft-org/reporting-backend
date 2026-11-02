from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from reporting.app.deps import PrincipalDep, ServicesDep
from reporting.auth import Action, ResourceRef
from reporting.core import AccessContext, ConnectionId, DatabaseRef, NotFound, TableRef
from reporting.metadata import (
    AddRelationship,
    JoinPath,
    RelationshipDef,
    TableDetail,
    TableSummary,
)

router = APIRouter(tags=["metadata"])


def _access(services: ServicesDep, principal: PrincipalDep, connection_id: str) -> AccessContext:
    return services.auth.access_context(principal, ConnectionId(connection_id))


@router.get("/connections/{connection_id}/databases", response_model=list[DatabaseRef])
def list_databases(connection_id: str, services: ServicesDep,
                   principal: PrincipalDep) -> list[DatabaseRef]:
    _access(services, principal, connection_id)
    return services.metadata.list_databases(connection_id)


class TablesResponse(BaseModel):
    snapshot_version: int
    tables: list[TableSummary]


@router.get("/connections/{connection_id}/databases/{database}/tables",
            response_model=TablesResponse)
def list_tables(connection_id: str, database: str, services: ServicesDep,
                principal: PrincipalDep) -> TablesResponse:
    access = _access(services, principal, connection_id)
    db = DatabaseRef(connection_id=connection_id, database=database)
    snap = services.metadata.snapshot(connection_id, database)
    tables = [t for t in services.metadata.list_tables(db) if access.table_visible(t.ref.key)]
    return TablesResponse(snapshot_version=snap.version, tables=tables)


@router.get("/tables", response_model=TableDetail, response_model_by_alias=True)
def get_table(
    services: ServicesDep, principal: PrincipalDep,
    connection_id: str, database: str, name: str,
    schema: Annotated[str | None, Query()] = None,
) -> TableDetail:
    access = _access(services, principal, connection_id)
    ref = TableRef(connection_id=connection_id, database=database, schema_=schema, name=name)
    if not access.table_visible(ref.key):
        raise NotFound(f"Table {ref.key} not found")
    detail = services.metadata.get_table(ref)
    # Hidden columns and relationships to hidden tables never reach the client.
    return detail.model_copy(update={
        "columns": tuple(c for c in detail.columns if access.column_visible(ref.key, c.name)),
        "outgoing": tuple(r for r in detail.outgoing if access.table_visible(r.related_table.key)),
        "incoming": tuple(r for r in detail.incoming if access.table_visible(r.related_table.key)),
    })


@router.get("/join-paths", response_model=list[JoinPath])
def join_paths(
    services: ServicesDep, principal: PrincipalDep,
    connection_id: str, database: str,
    source: Annotated[str, Query(alias="from")], target: Annotated[str, Query(alias="to")],
    max_hops: int = 3,
) -> list[JoinPath]:
    access = _access(services, principal, connection_id)
    snap = services.metadata.snapshot(connection_id, database)

    def ref(key: str) -> TableRef:
        if snap.table(key) is None or not access.table_visible(key):
            raise NotFound(f"Table {key} not found")
        return snap.table_ref(key)

    paths = services.metadata.find_join_paths(ref(source), ref(target), max_hops)
    return [p for p in paths if all(access.table_visible(t) for t in p.tables)]


@router.post("/connections/{connection_id}/databases/{database}/refresh")
def refresh(connection_id: str, database: str, services: ServicesDep,
            principal: PrincipalDep) -> dict[str, int]:
    services.auth.authorize(principal, Action.METADATA_REFRESH,
                            ResourceRef.connection(connection_id))
    return {"snapshot_version": services.metadata.refresh(connection_id, database)}


@router.post("/connections/{connection_id}/databases/{database}/relationships",
             response_model=RelationshipDef, status_code=201)
def add_relationship(connection_id: str, database: str, body: AddRelationship,
                     services: ServicesDep, principal: PrincipalDep) -> RelationshipDef:
    services.auth.authorize(principal, Action.RELATIONSHIP_CREATE,
                            ResourceRef.connection(connection_id))
    return services.metadata.add_user_relationship(
        DatabaseRef(connection_id=connection_id, database=database), body)
