def example_spec(connection_id: str, **overrides):
    """The plan's §3.1 example, adapted to the SQLite demo schema (no schema name)."""
    spec = {
        "version": 1,
        "connection_id": connection_id,
        "base": {"alias": "o", "table": {"database": "main", "name": "orders"}},
        "joins": [{"alias": "c", "from_alias": "o",
                   "relationship_id": "fk_orders_customer_id", "type": "left"}],
        "columns": [
            {"id": "country", "kind": "field", "ref": "c.country", "label": "Country"},
            {"id": "revenue", "kind": "aggregate", "fn": "sum", "arg": "o.total_amount",
             "label": "Revenue"},
            {"id": "orders", "kind": "aggregate", "fn": "count", "arg": "o.id",
             "label": "Orders"},
            {"id": "avg_ship", "kind": "aggregate", "fn": "avg",
             "arg": {"kind": "date_diff", "unit": "day", "start": "o.ordered_at",
                     "end": "o.shipped_at"},
             "label": "Avg days to ship"},
            {"id": "aov", "kind": "expression", "expr": "[revenue] / NULLIF([orders], 0)",
             "label": "Avg order value"},
        ],
        "filters": [
            {"ref": "o.status", "op": "in", "value": ["paid", "shipped"]},
            {"ref": "o.ordered_at", "op": "between", "value": ["2026-01-01", "2026-09-30"]},
        ],
        "having": [{"ref": "revenue", "op": "gt", "value": 50}],
        "group_by": "auto",
        "order_by": [{"ref": "revenue", "dir": "desc"}],
        "limit": 1000,
    }
    spec.update(overrides)
    return spec
