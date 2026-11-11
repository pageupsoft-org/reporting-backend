"""Assemble a snapshot from catalog query results, and diff snapshots."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from reporting.core import Dialect
from reporting.metadata._internal.types import map_native_type
from reporting.metadata.api import (
    ColumnChange,
    ColumnInfo,
    RelationshipDef,
    SchemaSnapshot,
    SnapshotDiff,
    TableSchema,
)


def _key(schema: Any, name: str) -> str:
    return f"{schema}.{name}" if isinstance(schema, str) and schema else name


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.astype(object).where(pd.notna(frame), None)
    return list(clean.to_dict(orient="records"))

# Bump when introspection or type mapping changes, so stored snapshots get re-read.
# 2: PostgreSQL enums read as text, domains as their base type.
SNAPSHOT_FORMAT = 2


def build_snapshot(
    *, connection_id: str, database: str, dialect: Dialect, version: int,
    tables: pd.DataFrame, columns: pd.DataFrame, keys: pd.DataFrame,
    row_counts: dict[str, int] | None = None,
    user_relationships: list[RelationshipDef] | None = None,
) -> SchemaSnapshot:
    key_rows = _records(keys)
    pk_cols: dict[str, set[str]] = defaultdict(set)
    for r in key_rows:
        if r["kind"] == "p":
            pk_cols[_key(r["schema_name"], r["table_name"])].add(r["column_name"])

    cols_by_table: dict[str, list[tuple[int, ColumnInfo]]] = defaultdict(list)
    for r in _records(columns):
        tkey = _key(r["schema_name"], r["table_name"])
        native = str(r["native_type"] or "")
        cols_by_table[tkey].append((int(r["ordinal"]), ColumnInfo(
            name=r["column_name"],
            native_type=native,
            data_type=map_native_type(dialect, native),
            nullable=bool(r["nullable"]),
            primary_key=r["column_name"] in pk_cols.get(tkey, set()),
        )))

    table_list: list[TableSchema] = []
    for r in _records(tables):
        tkey = _key(r["schema_name"], r["table_name"])
        estimate = r["row_estimate"]
        if row_counts is not None:
            estimate = row_counts.get(tkey)
        table_list.append(TableSchema(
            schema_name=r["schema_name"] or None,
            name=r["table_name"],
            kind="view" if r["kind"] == "view" else "table",
            row_estimate=int(estimate) if estimate is not None and estimate >= 0 else None,
            columns=tuple(c for _, c in sorted(cols_by_table.get(tkey, []), key=lambda x: x[0])),
        ))
    table_list.sort(key=lambda t: ((t.schema_name or ""), t.name.lower()))
    by_key = {t.key: t for t in table_list}

    declared = _declared_relationships(key_rows, by_key, dialect)
    user = [r for r in (user_relationships or [])
            if r.from_table in by_key and r.to_table in by_key]
    inferred = _infer_relationships(by_key, declared + user)
    rels = _unique_ids(declared + user + inferred)

    return SchemaSnapshot(
        connection_id=connection_id, database=database, dialect=dialect.value, version=version,
        created_at=datetime.now(UTC), tables=tuple(table_list), relationships=tuple(rels),
        format=SNAPSHOT_FORMAT,
    )


def _declared_relationships(key_rows: list[dict[str, Any]], by_key: dict[str, TableSchema],
                            dialect: Dialect) -> list[RelationshipDef]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in key_rows:
        if r["kind"] == "f":
            groups[(_key(r["schema_name"], r["table_name"]), r["constraint_name"])].append(r)
    rels: list[RelationshipDef] = []
    for (child, cname), rows in groups.items():
        rows.sort(key=lambda r: int(r["position"]))
        parent = _key(rows[0]["ref_schema"], rows[0]["ref_table"])
        if child not in by_key or parent not in by_key:
            continue
        parent_pk = by_key[parent].primary_key
        pairs = []
        for i, r in enumerate(rows):
            ref_col = r["ref_column"] or (parent_pk[i] if i < len(parent_pk) else None)
            if ref_col is None:
                break
            pairs.append((r["column_name"], ref_col))
        else:
            if dialect is Dialect.SQLITE:
                cname = "fk_" + by_key[child].name + "_" + "_".join(p[0] for p in pairs)
            child_pk = set(by_key[child].primary_key)
            one_to_one = bool(child_pk) and child_pk == {p[0] for p in pairs}
            rels.append(RelationshipDef(
                id=cname, constraint_name=cname, from_table=child, to_table=parent,
                column_pairs=tuple(pairs),
                cardinality="one_to_one" if one_to_one else "many_to_one",
                source="declared",
            ))
    return rels


def _singular(name: str) -> set[str]:
    n = name.lower()
    forms = {n}
    if n.endswith("ies"):
        forms.add(n[:-3] + "y")
    if n.endswith("es"):
        forms.add(n[:-2])
    if n.endswith("s"):
        forms.add(n[:-1])
    return forms


def _infer_relationships(by_key: dict[str, TableSchema],
                         existing: list[RelationshipDef]) -> list[RelationshipDef]:
    """``<table>_id`` / ``<table>id`` columns whose type matches a single-column PK."""
    covered = {(r.from_table, r.column_pairs[0][0]) for r in existing if len(r.column_pairs) == 1}
    candidates: dict[str, list[TableSchema]] = defaultdict(list)
    for t in by_key.values():
        if len(t.primary_key) == 1:
            for form in _singular(t.name):
                candidates[form].append(t)
    out: list[RelationshipDef] = []
    for child in by_key.values():
        for col in child.columns:
            if (child.key, col.name) in covered:
                continue
            m = re.fullmatch(r"(.+?)_?id", col.name.lower())
            if not m:
                continue
            for parent in candidates.get(m.group(1), []):
                pk_col = parent.column(parent.primary_key[0])
                if parent.key == child.key or pk_col is None:
                    continue
                if pk_col.data_type != col.data_type:
                    continue
                name = f"inf_{child.name}_{col.name}"
                out.append(RelationshipDef(
                    id=name, constraint_name=name, from_table=child.key, to_table=parent.key,
                    column_pairs=((col.name, pk_col.name),), source="inferred",
                ))
    return out


def _unique_ids(rels: list[RelationshipDef]) -> list[RelationshipDef]:
    seen: set[str] = set()
    out = []
    for r in rels:
        rid = r.id
        if rid in seen:
            rid = f"{r.from_table}:{r.id}"
        seen.add(rid)
        out.append(r if rid == r.id else r.model_copy(update={"id": rid}))
    return out


def content_signature(s: SchemaSnapshot) -> str:
    """Structure-only fingerprint (ignores version, timestamp and row estimates)."""
    tables = [t.model_copy(update={"row_estimate": None}) for t in s.tables]
    return "|".join(t.model_dump_json() for t in tables) + "#" + "|".join(
        r.model_dump_json() for r in s.relationships)


def diff_snapshots(old: SchemaSnapshot, new: SchemaSnapshot) -> SnapshotDiff:
    old_t = {t.key: t for t in old.tables}
    new_t = {t.key: t for t in new.tables}
    changes: list[ColumnChange] = []
    for key in sorted(old_t.keys() & new_t.keys()):
        oc = {c.name: c for c in old_t[key].columns}
        nc = {c.name: c for c in new_t[key].columns}
        changes += [ColumnChange(table=key, column=c, change="removed")
                    for c in sorted(oc.keys() - nc.keys())]
        changes += [ColumnChange(table=key, column=c, change="added")
                    for c in sorted(nc.keys() - oc.keys())]
        for c in sorted(oc.keys() & nc.keys()):
            if oc[c].data_type != nc[c].data_type:
                changes.append(ColumnChange(
                    table=key, column=c, change="retyped",
                    detail=f"{oc[c].data_type.value} → {nc[c].data_type.value}",
                ))
    return SnapshotDiff(
        from_version=old.version, to_version=new.version,
        removed_tables=tuple(sorted(old_t.keys() - new_t.keys())),
        added_tables=tuple(sorted(new_t.keys() - old_t.keys())),
        column_changes=tuple(changes),
    )
