"""Snapshot-backed implementation of ``MetadataService``."""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError

from reporting.connections import ConnectionService
from reporting.core import (
    CompiledStatement,
    ConnectionId,
    DatabaseRef,
    Dialect,
    ExecutionLimits,
    Issue,
    NotFound,
    SnapshotVersion,
    TableRef,
    ValidationFailed,
    new_id,
)
from reporting.core.db import create_app_engine, create_tables
from reporting.metadata._internal.builder import (
    SNAPSHOT_FORMAT,
    build_snapshot,
    content_signature,
    diff_snapshots,
)
from reporting.metadata._internal.graph import find_paths
from reporting.metadata._internal.introspectors import QUERIES
from reporting.metadata.api import (
    AddRelationship,
    Cardinality,
    Direction,
    JoinPath,
    Relationship,
    RelationshipDef,
    SchemaSnapshot,
    SnapshotDiff,
    TableDetail,
    TableSummary,
)

log = logging.getLogger(__name__)

CATALOG_LIMITS = ExecutionLimits(timeout_s=60.0, max_rows=2_000_000)
FRESHNESS_S = 2.0  # how stale a process's view of the latest snapshot may be
MAX_COUNTED_TABLES = 200
UPGRADE_RETRY_S = 300.0  # wait before retrying a failed snapshot upgrade


class SnapshotMetadataService:
    def __init__(self, app_db_url: str, connections: ConnectionService) -> None:
        self._connections = connections
        self._app = create_app_engine(app_db_url)
        md = MetaData()
        self._snapshots = Table(
            "metadata_snapshots", md,
            Column("connection_id", String(32), primary_key=True),
            Column("database", String(255), primary_key=True),
            Column("version", Integer, primary_key=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("payload", Text, nullable=False),
        )
        self._user_rels = Table(
            "metadata_user_relationships", md,
            Column("id", String(40), primary_key=True),
            Column("connection_id", String(32), nullable=False, index=True),
            Column("database", String(255), nullable=False),
            Column("payload", Text, nullable=False),
        )
        create_tables(self._app, md)
        self._latest: dict[tuple[str, str], SchemaSnapshot] = {}
        self._checked: dict[tuple[str, str], float] = {}
        self._upgrade_failed: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    # -- databases and tables -------------------------------------------------------------

    def list_databases(self, connection_id: str) -> list[DatabaseRef]:
        dialect = self._connections.dialect(ConnectionId(connection_id))
        result = self._connections.execute_read(
            ConnectionId(connection_id), CompiledStatement(QUERIES[dialect].databases),
            CATALOG_LIMITS,
        )
        return [DatabaseRef(connection_id=connection_id, database=str(n))
                for n in result.frame["name"]]

    def list_tables(self, db: DatabaseRef) -> list[TableSummary]:
        snap = self.snapshot(db.connection_id, db.database)
        return [TableSummary(ref=snap.table_ref(t.key), kind=t.kind, row_estimate=t.row_estimate)
                for t in snap.tables]

    def get_table(self, table: TableRef) -> TableDetail:
        snap = self.snapshot(table.connection_id, table.database)
        t = snap.table(table.key)
        if t is None:
            raise NotFound(f"Table {table.key} not found in {table.database}")
        rels = self.related_tables(table)
        return TableDetail(
            ref=snap.table_ref(t.key), kind=t.kind, row_estimate=t.row_estimate,
            columns=t.columns,
            outgoing=tuple(r for r in rels if r.direction == "outgoing"),
            incoming=tuple(r for r in rels if r.direction == "incoming"),
        )

    def related_tables(self, table: TableRef) -> list[Relationship]:
        snap = self.snapshot(table.connection_id, table.database)
        if snap.table(table.key) is None:
            raise NotFound(f"Table {table.key} not found in {table.database}")
        out: list[Relationship] = []
        for r in snap.relationships:
            if r.from_table == table.key:
                out.append(self._view(snap, r, "outgoing"))
            if r.to_table == table.key:
                out.append(self._view(snap, r, "incoming"))
        return out

    @staticmethod
    def _view(snap: SchemaSnapshot, r: RelationshipDef, direction: Direction) -> Relationship:
        card: Cardinality
        if direction == "outgoing":
            card = r.cardinality
            related = r.to_table
        else:
            card = "one_to_one" if r.cardinality == "one_to_one" else "one_to_many"
            related = r.from_table
        return Relationship(
            id=r.id, constraint_name=r.constraint_name,
            from_table=snap.table_ref(r.from_table), to_table=snap.table_ref(r.to_table),
            related_table=snap.table_ref(related), column_pairs=r.column_pairs,
            direction=direction,
            cardinality=card, source=r.source,
        )

    def find_join_paths(self, source: TableRef, target: TableRef,
                        max_hops: int = 3) -> list[JoinPath]:
        if (source.connection_id, source.database) != (target.connection_id, target.database):
            raise ValidationFailed("Joins across databases are not supported")
        snap = self.snapshot(source.connection_id, source.database)
        for t in (source, target):
            if snap.table(t.key) is None:
                raise NotFound(f"Table {t.key} not found in {t.database}")
        return find_paths(snap, source.key, target.key, max(1, min(max_hops, 5)))

    # -- snapshots ------------------------------------------------------------------------

    def _default_db(self, connection_id: str) -> str:
        info = self._connections.list({ConnectionId(connection_id)})
        if not info:
            raise NotFound(f"Connection {connection_id} not found")
        if info[0].dialect is Dialect.SQLITE:
            return "main"
        if info[0].default_database:
            return info[0].default_database
        dbs = self.list_databases(connection_id)
        if not dbs:
            raise NotFound("Connection exposes no databases")
        return dbs[0].database

    def snapshot(self, connection_id: str, database: str | None = None,
                 version: int | None = None) -> SchemaSnapshot:
        database = database or self._default_db(connection_id)
        if version is not None:
            return self._load(connection_id, database, version)
        key = (connection_id, database)
        # Other server processes may have refreshed: re-check the stored version (a cheap
        # indexed lookup) at most every FRESHNESS_S seconds.
        now = time.monotonic()
        if key not in self._latest or now - self._checked.get(key, 0.0) > FRESHNESS_S:
            stored = self._load_latest(connection_id, database)
            if stored is None:
                self.refresh(connection_id, database)
            elif stored.format < SNAPSHOT_FORMAT and self._upgrade(key, now):
                pass  # re-read with the current introspection; refresh() cached it
            elif key not in self._latest or self._latest[key].version != stored.version:
                self._latest[key] = stored
            self._checked[key] = now
        return self._latest[key]

    def _upgrade(self, key: tuple[str, str], now: float) -> bool:
        """Re-read a snapshot stored by an older introspection format. When the database
        can't be reached, keep using the stored one and retry later."""
        if now - self._upgrade_failed.get(key, -UPGRADE_RETRY_S) < UPGRADE_RETRY_S:
            return False
        connection_id, database = key
        try:
            self.refresh(connection_id, database)
            return True
        except Exception:  # noqa: BLE001 - an unreachable database must not block reads
            self._upgrade_failed[key] = now
            log.warning("could not upgrade schema snapshot of %s/%s", connection_id, database,
                        exc_info=True)
            return False

    def refresh(self, connection_id: str, database: str | None = None) -> SnapshotVersion:
        database = database or self._default_db(connection_id)
        with self._lock:
            previous = self._load_latest(connection_id, database)
            version = (previous.version + 1) if previous else 1
            fresh = self._introspect(connection_id, database, version)
            if (previous is not None and previous.format == fresh.format
                    and content_signature(previous) == content_signature(fresh)):
                # Unchanged structure keeps its version so in-flight specs stay valid; row
                # estimates are refreshed in memory.
                self._latest[(connection_id, database)] = fresh.model_copy(
                    update={"version": previous.version, "created_at": previous.created_at})
                return SnapshotVersion(previous.version)
            try:
                with self._app.begin() as conn:
                    conn.execute(insert(self._snapshots).values(
                        connection_id=connection_id, database=database, version=version,
                        created_at=fresh.created_at, payload=fresh.model_dump_json(),
                    ))
            except IntegrityError:
                # Another server process stored this version first; use what it stored.
                winner = self._load_latest(connection_id, database)
                assert winner is not None
                self._latest[(connection_id, database)] = winner
                return SnapshotVersion(winner.version)
            self._latest[(connection_id, database)] = fresh
            self._checked[(connection_id, database)] = time.monotonic()
            return SnapshotVersion(version)

    def diff(self, connection_id: str, database: str, from_version: int,
             to_version: int) -> SnapshotDiff:
        return diff_snapshots(self._load(connection_id, database, from_version),
                              self._load(connection_id, database, to_version))

    def add_user_relationship(self, db: DatabaseRef, req: AddRelationship) -> RelationshipDef:
        snap = self.snapshot(db.connection_id, db.database)
        issues: list[Issue] = []
        child, parent = snap.table(req.from_table), snap.table(req.to_table)
        if child is None:
            issues.append(Issue("from_table", f"Unknown table {req.from_table}"))
        if parent is None:
            issues.append(Issue("to_table", f"Unknown table {req.to_table}"))
        if not req.column_pairs:
            issues.append(Issue("column_pairs", "At least one column pair is required"))
        if child and parent:
            for i, (a, b) in enumerate(req.column_pairs):
                ca, cb = child.column(a), parent.column(b)
                if ca is None or cb is None:
                    issues.append(Issue(f"column_pairs[{i}]", f"Unknown column {a} or {b}"))
                elif ca.data_type != cb.data_type:
                    issues.append(Issue(f"column_pairs[{i}]",
                                        f"Types differ: {ca.data_type} vs {cb.data_type}"))
        if issues:
            raise ValidationFailed("Invalid relationship", issues)
        rid = new_id("usr")
        rel = RelationshipDef(
            id=rid, constraint_name=req.name or rid, from_table=req.from_table,
            to_table=req.to_table, column_pairs=tuple(req.column_pairs), source="user",
        )
        with self._app.begin() as conn:
            conn.execute(insert(self._user_rels).values(
                id=rid, connection_id=db.connection_id, database=db.database,
                payload=rel.model_dump_json()))
        self.refresh(db.connection_id, db.database)
        return rel

    # -- internals ------------------------------------------------------------------------

    def _introspect(self, connection_id: str, database: str, version: int) -> SchemaSnapshot:
        cid = ConnectionId(connection_id)
        dialect = self._connections.dialect(cid)
        q = QUERIES[dialect]

        def run(sql: str):  # type: ignore[no-untyped-def]
            params = {"db": database} if ":db" in sql else {}
            return self._connections.execute_read(
                cid, CompiledStatement(sql, params, database=database), CATALOG_LIMITS).frame

        tables, columns, keys = run(q.tables), run(q.columns), run(q.keys)
        row_counts = None
        if q.row_counts and 0 < len(tables) <= MAX_COUNTED_TABLES:
            names = [n for n, k in zip(tables["table_name"], tables["kind"], strict=True)
                     if k == "table"]
            if names:
                union = " UNION ALL ".join(
                    "SELECT {lit} AS t, COUNT(*) AS n FROM {ident}".format(
                        lit="'" + n.replace("'", "''") + "'",
                        ident='"' + n.replace('"', '""') + '"')
                    for n in names)
                counts = run(union)
                row_counts = {str(t): int(n) for t, n in zip(counts["t"], counts["n"], strict=True)}
        return build_snapshot(
            connection_id=connection_id, database=database, dialect=dialect, version=version,
            tables=tables, columns=columns, keys=keys, row_counts=row_counts,
            user_relationships=self._user_relationships(connection_id, database),
        )

    def _user_relationships(self, connection_id: str, database: str) -> list[RelationshipDef]:
        stmt = select(self._user_rels.c.payload).where(
            self._user_rels.c.connection_id == connection_id,
            self._user_rels.c.database == database)
        with self._app.connect() as conn:
            return [RelationshipDef.model_validate_json(p) for (p,) in conn.execute(stmt)]

    def _load_latest(self, connection_id: str, database: str) -> SchemaSnapshot | None:
        t = self._snapshots
        with self._app.connect() as conn:
            latest = conn.execute(select(func.max(t.c.version)).where(
                t.c.connection_id == connection_id, t.c.database == database)).scalar()
        if latest is None:
            return None
        cached = self._latest.get((connection_id, database))
        if cached is not None and cached.version == latest:
            return cached
        return self._load(connection_id, database, int(latest))

    def _load(self, connection_id: str, database: str, version: int) -> SchemaSnapshot:
        t = self._snapshots
        with self._app.connect() as conn:
            payload = conn.execute(select(t.c.payload).where(
                t.c.connection_id == connection_id, t.c.database == database,
                t.c.version == version)).scalar()
        if payload is None:
            raise NotFound(f"Snapshot version {version} of {database} not found")
        return SchemaSnapshot.model_validate(json.loads(payload))
