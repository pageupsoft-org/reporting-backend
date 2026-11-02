"""Relationship graph: FKs as an undirected graph with typed edges."""

from __future__ import annotations

from collections import defaultdict, deque

from reporting.metadata.api import Cardinality, JoinPath, JoinStep, SchemaSnapshot


def _edges(snapshot: SchemaSnapshot) -> dict[str, list[JoinStep]]:
    adj: dict[str, list[JoinStep]] = defaultdict(list)
    for r in snapshot.relationships:
        forward: Cardinality = "one_to_one" if r.cardinality == "one_to_one" else "many_to_one"
        backward: Cardinality = "one_to_one" if r.cardinality == "one_to_one" else "one_to_many"
        adj[r.from_table].append(JoinStep(
            relationship_id=r.id, left_table=r.from_table, right_table=r.to_table,
            column_pairs=r.column_pairs, cardinality=forward,
        ))
        if r.to_table != r.from_table:
            adj[r.to_table].append(JoinStep(
                relationship_id=r.id, left_table=r.to_table, right_table=r.from_table,
                column_pairs=tuple((b, a) for a, b in r.column_pairs), cardinality=backward,
            ))
    return adj


def find_paths(snapshot: SchemaSnapshot, source: str, target: str,
               max_hops: int = 3) -> list[JoinPath]:
    """Breadth-first search returning every shortest simple path up to ``max_hops``."""
    if source == target:
        return [JoinPath(tables=(source,), steps=())]
    adj = _edges(snapshot)
    frontier: deque[tuple[str, tuple[JoinStep, ...]]] = deque([(source, ())])
    found: list[JoinPath] = []
    depth_found: int | None = None
    while frontier:
        node, path = frontier.popleft()
        if depth_found is not None and len(path) >= depth_found:
            continue
        if len(path) >= max_hops:
            continue
        visited = {source, *(s.right_table for s in path)}
        for step in adj.get(node, []):
            if step.right_table in visited:
                continue
            new_path = (*path, step)
            if step.right_table == target:
                depth_found = len(new_path)
                found.append(JoinPath(
                    tables=(source, *(s.right_table for s in new_path)), steps=new_path))
            else:
                frontier.append((step.right_table, new_path))
    return found
