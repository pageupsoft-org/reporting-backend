"""Public API of the metadata module: introspects schemas and answers relationship questions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import PrivateAttr

from reporting.core import DatabaseRef, DataType, Frozen, SnapshotVersion, TableRef

if TYPE_CHECKING:
    from reporting.connections import ConnectionService

Direction = Literal["outgoing", "incoming"]
Cardinality = Literal["many_to_one", "one_to_many", "one_to_one"]
RelationshipSource = Literal["declared", "inferred", "user"]
TableKind = Literal["table", "view"]


class ColumnInfo(Frozen):
    name: str
    native_type: str
    data_type: DataType
    nullable: bool
    primary_key: bool = False


class TableSchema(Frozen):
    """A table as stored inside a snapshot (keyed by ``schema.name``)."""

    schema_name: str | None
    name: str
    kind: TableKind = "table"
    row_estimate: int | None = None
    columns: tuple[ColumnInfo, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.schema_name}.{self.name}" if self.schema_name else self.name

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.primary_key)


class RelationshipDef(Frozen):
    """Canonical relationship: ``from_table`` (child, holds the FK) → ``to_table`` (parent)."""

    id: str
    constraint_name: str
    from_table: str
    to_table: str
    column_pairs: tuple[tuple[str, str], ...]
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"
    source: RelationshipSource = "declared"


class SchemaSnapshot(Frozen):
    """Immutable, versioned schema of one database of one connection."""

    connection_id: str
    database: str
    dialect: str
    version: int
    created_at: datetime
    tables: tuple[TableSchema, ...]
    relationships: tuple[RelationshipDef, ...]

    _tables: dict[str, TableSchema] = PrivateAttr(default_factory=dict)
    _rels: dict[str, RelationshipDef] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        self._tables = {t.key: t for t in self.tables}
        self._rels = {r.id: r for r in self.relationships}

    def table(self, key: str) -> TableSchema | None:
        return self._tables.get(key)

    def relationship(self, rel_id: str) -> RelationshipDef | None:
        return self._rels.get(rel_id)

    def table_ref(self, key: str) -> TableRef:
        t = self._tables[key]
        return TableRef(connection_id=self.connection_id, database=self.database,
                        schema_=t.schema_name, name=t.name)


class TableSummary(Frozen):
    ref: TableRef
    kind: TableKind
    row_estimate: int | None


class Relationship(Frozen):
    """A relationship seen from one table (``direction`` and ``cardinality`` are relative)."""

    id: str
    constraint_name: str
    from_table: TableRef
    to_table: TableRef
    related_table: TableRef
    column_pairs: tuple[tuple[str, str], ...]
    direction: Direction
    cardinality: Cardinality
    source: RelationshipSource


class TableDetail(Frozen):
    ref: TableRef
    kind: TableKind
    row_estimate: int | None
    columns: tuple[ColumnInfo, ...]
    outgoing: tuple[Relationship, ...]
    incoming: tuple[Relationship, ...]


class JoinStep(Frozen):
    relationship_id: str
    left_table: str
    right_table: str
    column_pairs: tuple[tuple[str, str], ...]  # (left column, right column)
    cardinality: Cardinality  # from left to right


class JoinPath(Frozen):
    tables: tuple[str, ...]  # breadcrumb, e.g. ("orders", "customers", "regions")
    steps: tuple[JoinStep, ...]


class ColumnChange(Frozen):
    table: str
    column: str
    change: Literal["removed", "added", "retyped"]
    detail: str = ""


class SnapshotDiff(Frozen):
    from_version: int
    to_version: int
    removed_tables: tuple[str, ...] = ()
    added_tables: tuple[str, ...] = ()
    column_changes: tuple[ColumnChange, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.removed_tables or self.added_tables or self.column_changes)

    def describe(self) -> list[str]:
        lines = [f"table {t} was removed" for t in self.removed_tables]
        lines += [f"table {t} was added" for t in self.added_tables]
        lines += [
            f"column {c.table}.{c.column} was {c.change}" + (f" ({c.detail})" if c.detail else "")
            for c in self.column_changes
        ]
        return lines


class AddRelationship(Frozen):
    from_table: str
    to_table: str
    column_pairs: tuple[tuple[str, str], ...]
    name: str | None = None


class MetadataService(Protocol):
    def list_databases(self, connection_id: str) -> list[DatabaseRef]: ...
    def list_tables(self, db: DatabaseRef) -> list[TableSummary]: ...
    def get_table(self, table: TableRef) -> TableDetail: ...
    def related_tables(self, table: TableRef) -> list[Relationship]: ...
    def find_join_paths(self, source: TableRef, target: TableRef,
                        max_hops: int = 3) -> list[JoinPath]: ...
    def snapshot(self, connection_id: str, database: str | None = None,
                 version: int | None = None) -> SchemaSnapshot: ...
    def refresh(self, connection_id: str, database: str | None = None) -> SnapshotVersion: ...
    def diff(self, connection_id: str, database: str, from_version: int,
             to_version: int) -> SnapshotDiff: ...
    def add_user_relationship(self, db: DatabaseRef, req: AddRelationship) -> RelationshipDef: ...


def create_metadata_service(app_db_url: str, connections: ConnectionService) -> MetadataService:
    from reporting.metadata._internal.service import SnapshotMetadataService

    return SnapshotMetadataService(app_db_url, connections)


def join_paths(snapshot: SchemaSnapshot, source: str, target: str,
               max_hops: int = 3) -> list[JoinPath]:
    """Pure helper: all shortest join paths between two table keys in a snapshot."""
    from reporting.metadata._internal.graph import find_paths

    return find_paths(snapshot, source, target, max_hops)


__all__ = [
    "AddRelationship",
    "Cardinality",
    "ColumnChange",
    "ColumnInfo",
    "Direction",
    "JoinPath",
    "JoinStep",
    "MetadataService",
    "Relationship",
    "RelationshipDef",
    "SchemaSnapshot",
    "SnapshotDiff",
    "TableDetail",
    "TableSchema",
    "TableSummary",
    "create_metadata_service",
    "join_paths",
]
